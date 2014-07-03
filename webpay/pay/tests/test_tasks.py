# -*- coding: utf-8 -*-
import urllib2
from urllib import urlencode

from django import test
from django.conf import settings
from django.core.exceptions import ObjectDoesNotExist
from django.test import RequestFactory

import fudge
from fudge.inspector import arg
import jwt
import mock
from mock import ANY
from nose.exc import SkipTest
from nose.tools import eq_, ok_, raises
from requests.exceptions import RequestException, Timeout
import test_utils


from lib.marketplace.api import client, UnknownPricePoint
from lib.marketplace.constants import COUNTRIES
from lib.solitude import api
from lib.solitude import constants
from webpay.base.utils import gmtime
from webpay.constants import TYP_CHARGEBACK, TYP_POSTBACK
from webpay.pay import tasks
from webpay.pay.samples import JWTtester

from .test_views import sample


class NotifyTest(JWTtester, test.TestCase):

    def setUp(self):
        super(NotifyTest, self).setUp()
        self.trans_uuid = 'trans_uuid'
        self.buyer_uuid = 'buyer_uuid'

    def set_secret_mock(self, slumber, s):
        slumber.generic.product.get_object_or_404.return_value = {'secret': s}

    def url(self, path, protocol='https'):
        return protocol + '://' + self.domain + path


class TestNotifyApp(NotifyTest):

    @mock.patch('lib.solitude.api.client.get_transaction')
    def do_chargeback(self, reason, get_transaction):
        get_transaction.return_value = {
            'amount': 1,
            'currency': 'USD',
            'status': constants.STATUS_COMPLETED,
            'notes': {'pay_request': self.payload(),
                      'issuer_key': 'k'},
            'type': constants.TYPE_REFUND,
            'uuid': self.trans_uuid
        }
        with self.settings(INAPP_KEY_PATHS={None: sample}, DEBUG=True):
            tasks.chargeback_notify(self.trans_uuid, reason=reason)

    @mock.patch('lib.solitude.api.client.get_transaction')
    def notify(self, get_transaction):
        get_transaction.return_value = {
            'amount': 1,
            'currency': 'USD',
            'status': constants.STATUS_COMPLETED,
            'notes': {'pay_request': self.payload(),
                      'issuer_key': 'k'},
            'type': constants.TYPE_PAYMENT,
            'uuid': self.trans_uuid,
        }
        with self.settings(INAPP_KEY_PATHS={None: sample}, DEBUG=True):
            tasks.payment_notify(self.trans_uuid, self.buyer_uuid)

    @fudge.patch('webpay.pay.utils.requests')
    @mock.patch('lib.solitude.api.client.slumber')
    def test_notify_pay(self, fake_req, slumber):
        self.set_secret_mock(slumber, 'f')
        payload = self.payload(typ=TYP_POSTBACK)
        url = payload['request']['postbackURL']

        def req_ok(req):
            dd = jwt.decode(req['notice'], verify=False)
            eq_(dd['request'], payload['request'])
            eq_(dd['typ'], payload['typ'])
            eq_(dd['response']['price']['amount'], 1)
            eq_(dd['response']['price']['currency'], u'USD')
            eq_(dd['response']['buyer_uuid'], self.buyer_uuid)
            jwt.decode(req['notice'], 'f', verify=True)
            return True

        (fake_req.expects('post').with_args(url, arg.passes_test(req_ok),
                                            timeout=5)
                                 .returns_fake()
                                 .has_attr(text=self.trans_uuid)
                                 .expects('raise_for_status'))
        self.notify()

    @fudge.patch('webpay.pay.utils.requests')
    @mock.patch('lib.solitude.api.client.slumber')
    def test_notify_refund_chargeback(self, fake_req, slumber):
        self.set_secret_mock(slumber, 'f')
        payload = self.payload(typ=TYP_CHARGEBACK)
        url = payload['request']['chargebackURL']

        def req_ok(req):
            dd = jwt.decode(req['notice'], verify=False)
            eq_(dd['request'], payload['request'])
            eq_(dd['typ'], payload['typ'])
            eq_(dd['response']['transactionID'], self.trans_uuid)
            eq_(dd['response']['reason'], 'refund')
            jwt.decode(req['notice'], 'f', verify=True)
            return True

        (fake_req.expects('post').with_args(url, arg.passes_test(req_ok),
                                            timeout=5)
                                 .returns_fake()
                                 .has_attr(text=self.trans_uuid)
                                 .expects('raise_for_status'))
        self.do_chargeback('refund')

    @fudge.patch('webpay.pay.utils.requests')
    @mock.patch('lib.solitude.api.client.slumber')
    def test_notify_reversal_chargeback(self, fake_req, slumber):
        self.set_secret_mock(slumber, 'f')

        def req_ok(req):
            dd = jwt.decode(req['notice'], verify=False)
            eq_(dd['response']['reason'], 'reversal')
            return True

        (fake_req.expects('post').with_args('http://foo.url/charge',
                                            arg.passes_test(req_ok),
                                            timeout=5)
                                 .returns_fake()
                                 .has_attr(text=self.trans_uuid)
                                 .expects('raise_for_status'))
        self.do_chargeback('reversal')

    @mock.patch('webpay.pay.utils.requests')
    @mock.patch('lib.solitude.api.client.slumber')
    @mock.patch('lib.marketplace.api.client.api')
    def test_notify_marketplace(self, marketplace, solitude, requests):
        self.set_secret_mock(solitude, 'f')
        requests.post.side_effect = Timeout('Timeout')
        self.notify()
        assert marketplace.webpay.failure.called

    @mock.patch('webpay.pay.utils.requests')
    @mock.patch('lib.solitude.api.client.slumber')
    @mock.patch('lib.marketplace.api.client.api')
    def test_notify_timeout(self, marketplace, solitude, requests):
        self.set_secret_mock(solitude, 'f')
        requests.post.side_effect = Timeout('Timeout')
        self.notify()

    @mock.patch('lib.solitude.api.client.slumber')
    @mock.patch('webpay.pay.tasks.payment_notify.retry')
    @mock.patch('webpay.pay.utils.requests.post')
    def test_retry_http_error(self, post, retry, slumber):
        self.set_secret_mock(slumber, 'f')
        post.side_effect = RequestException('500 error')
        self.notify()
        assert post.called, 'notification not sent'
        assert retry.called, 'task was not retried after error'

    @fudge.patch('webpay.pay.utils.requests')
    @mock.patch('lib.solitude.api.client.slumber')
    @mock.patch('lib.marketplace.api.client.api')
    def test_any_error(self, fake_req, marketplace, solitude):
        self.set_secret_mock(solitude, 'f')
        fake_req.expects('post').raises(RequestException('some http error'))
        self.notify()

    @fudge.patch('webpay.pay.utils.requests')
    @mock.patch('lib.solitude.api.client.slumber')
    @mock.patch('lib.marketplace.api.client.api')
    def test_bad_status(self, fake_req, marketplace, solitude):
        self.set_secret_mock(solitude, 'f')
        (fake_req.expects('post').returns_fake()
                                 .has_attr(text='')
                                 .expects('raise_for_status')
                                 .raises(urllib2.HTTPError('url', 500, 'Error',
                                                           [], None)))
        self.notify()

    @mock.patch('lib.solitude.api.client.slumber')
    @mock.patch('webpay.pay.utils.requests')
    @mock.patch('webpay.pay.tasks.payment_notify.retry')
    def test_notify_retries(self, retry, requests, slumber):
        self.set_secret_mock(slumber, 'f')
        requests.post.side_effect = RequestException('some http error')
        self.notify()
        assert retry.called, 'task was not retried after error'

    @mock.patch('lib.solitude.api.client.slumber')
    @mock.patch('webpay.pay.utils.requests')
    @mock.patch('webpay.pay.tasks.payment_notify.retry')
    def test_notify_wrong(self, retry, requests, slumber):
        self.set_secret_mock(slumber, 'f')
        requests.post.return_value.content = '<not a valid response>'
        self.notify()
        assert retry.called, 'task was not retried after error'

    @mock.patch('lib.solitude.api.client.slumber')
    @mock.patch('webpay.pay.utils.requests')
    @mock.patch('webpay.pay.utils.notify_failure')
    def test_failure_notifies(self, notify, requests, slumber):
        self.set_secret_mock(slumber, 'f')
        requests.post.side_effect = RequestException('some http error')
        self.notify()
        assert notify.called, 'notify called'

    @fudge.patch('webpay.pay.utils.requests')
    @mock.patch('lib.solitude.api.client.slumber')
    def test_signed_app_response(self, fake_req, slumber):
        app_payment = self.payload()
        self.set_secret_mock(slumber, 'f')
        slumber.generic.product.get_object_or_404.return_value = {
            'secret': 'f'}

        # Ensure that the JWT sent to the app for payment notification
        # includes the same payment data that the app originally sent.
        def is_valid(payload):
            data = jwt.decode(payload['notice'], 'f',  # secret key
                              verify=True)
            eq_(data['iss'], settings.NOTIFY_ISSUER)
            eq_(data['typ'], TYP_POSTBACK)
            eq_(data['request']['pricePoint'], 1)
            eq_(data['request']['name'], app_payment['request']['name'])
            eq_(data['request']['description'],
                app_payment['request']['description'])
            eq_(data['request']['productdata'],
                app_payment['request']['productdata'])
            eq_(data['request']['postbackURL'], 'http://foo.url/post')
            eq_(data['request']['chargebackURL'], 'http://foo.url/charge')
            eq_(data['response']['transactionID'], self.trans_uuid)
            assert data['iat'] <= gmtime() + 60, (
                'Expected iat to be about now')
            assert data['exp'] > gmtime() + 3500, (
                'Expected exp to be about an hour from now')
            return True

        (fake_req.expects('post').with_args(arg.any(),
                                            arg.passes_test(is_valid),
                                            timeout=arg.any())
                                 .returns_fake()
                                 .has_attr(text=self.trans_uuid)
                                 .provides('raise_for_status'))
        self.notify()


@mock.patch('lib.solitude.api.client.slumber')
class TestSimulatedNotifications(NotifyTest):

    @mock.patch.object(client, 'get_price')
    def notify(self, payload, get_price, prices=None):
        get_price.return_value = (
            prices or {'prices': [{'price': '0.99', 'currency': 'USD'}]})
        tasks.simulate_notify('issuer-key', payload,
                              trans_uuid=self.trans_uuid)

    @fudge.patch('webpay.pay.utils.requests')
    def test_postback(self, slumber, fake_req):
        self.set_secret_mock(slumber, 'f')
        payload = self.payload(typ=TYP_POSTBACK,
                               extra_req={'simulate': {'result': 'postback'}})
        url = payload['request']['postbackURL']

        def req_ok(req):
            dd = jwt.decode(req['notice'], verify=False)
            eq_(dd['request'], payload['request'])
            eq_(dd['typ'], payload['typ'])
            jwt.decode(req['notice'], 'f', verify=True)
            return True

        (fake_req.expects('post').with_args(url, arg.passes_test(req_ok),
                                            timeout=arg.any())
                                 .returns_fake()
                                 .has_attr(text=self.trans_uuid)
                                 .expects('raise_for_status'))
        self.notify(payload)

    @fudge.patch('webpay.pay.utils.requests')
    def test_chargeback(self, slumber, fake_req):
        self.set_secret_mock(slumber, 'f')
        req = {'simulate': {'result': 'chargeback'}}
        payload = self.payload(typ=TYP_CHARGEBACK,
                               extra_req=req)
        url = payload['request']['chargebackURL']

        def req_ok(req):
            dd = jwt.decode(req['notice'], verify=False)
            eq_(dd['request'], payload['request'])
            eq_(dd['typ'], payload['typ'])
            jwt.decode(req['notice'], 'f', verify=True)
            return True

        (fake_req.expects('post').with_args(url, arg.passes_test(req_ok),
                                            timeout=arg.any())
                                 .returns_fake()
                                 .has_attr(text=self.trans_uuid)
                                 .expects('raise_for_status'))
        self.notify(payload)

    @fudge.patch('webpay.pay.utils.requests')
    def test_chargeback_reason(self, slumber, fake_req):
        self.set_secret_mock(slumber, 'f')
        reason = 'something'
        req = {'simulate': {'result': 'chargeback',
                            'reason': reason}}
        payload = self.payload(typ=TYP_CHARGEBACK,
                               extra_req=req)
        url = payload['request']['chargebackURL']

        def req_ok(req):
            dd = jwt.decode(req['notice'], verify=False)
            eq_(dd['request'], payload['request'])
            eq_(dd['response']['reason'], reason)
            return True

        (fake_req.expects('post').with_args(url, arg.passes_test(req_ok),
                                            timeout=arg.any())
                                 .returns_fake()
                                 .has_attr(text=self.trans_uuid)
                                 .expects('raise_for_status'))
        self.notify(payload)

    @mock.patch('webpay.pay.tasks.simulate_notify.retry')
    @mock.patch('webpay.pay.utils.requests.post')
    def test_retry_http_error(self, post, retry, slumber):
        self.set_secret_mock(slumber, 'f')
        post.side_effect = RequestException('500 error')

        req = {'simulate': {'result': 'postback'}}
        payload = self.payload(typ=TYP_POSTBACK,
                               extra_req=req)
        self.notify(payload)

        assert post.called, 'notification not sent'
        assert retry.called, 'task was not retried after error'
        retry.assert_called_with(args=['issuer-key', payload],
                                 max_retries=ANY, eta=ANY, exc=ANY)

    @mock.patch('webpay.pay.utils.requests.post')
    @mock.patch('webpay.pay.utils.notify_failure')
    def test_no_notifications_on_simulate(self, notify_failure, post, slumber):
        self.set_secret_mock(slumber, 'f')
        post.side_effect = RequestException('500 error')

        req = {'simulate': {'result': 'postback'}}
        payload = self.payload(typ=TYP_POSTBACK, extra_req=req)
        self.notify(payload)
        assert not notify_failure.called, 'Notification should not be sent'

    @raises(IndexError)
    @fudge.patch('webpay.pay.utils.requests')
    def test_no_tier(self, slumber, fake_req):
        self.set_secret_mock(slumber, 'f')
        payload = self.payload(typ=TYP_POSTBACK,
                               extra_req={'simulate': {'result': 'postback'}})

        (fake_req.expects('post').returns_fake()
                                 .has_attr(text=self.trans_uuid)
                                 .expects('raise_for_status'))
        self.notify(payload, prices={'prices': []})


class BaseStartPay(test_utils.TestCase):

    def setUp(self):
        self.issue = 'some-public-id'
        self.user_uuid = 'some-user-uuid'
        self.transaction_uuid = 'webpay:some-id'
        self.notes = {
            'issuer_key': self.issue,
            'pay_request': {
                'iss': 'some-seller-key',
                'request': {'pricePoint': 1,
                            'id': 'generated-product-uuid',
                            'icons': {'64': 'http://app/i.png'},
                            'name': 'Virtual Sword',
                            'description': 'A fancy sword'}}}
        self.prices = {'prices': [{'price': 1, 'currency': 'EUR'}]}


class TestStartPay(BaseStartPay):

    def setUp(self):
        super(TestStartPay, self).setUp()

        p = mock.patch('lib.solitude.api.client.slumber')
        self.solitude = p.start()
        self.addCleanup(p.stop)

        p = mock.patch('lib.marketplace.api.client.api')
        self.mkt = p.start()
        self.addCleanup(p.stop)

        self.providers = api.ProviderHelper.supported_providers()
        self.generic_seller_uuid = 'generic_seller_uuid'
        self.generic_seller_pk = '1'
        self.bango_seller_uuid = 'bango_seller_uuid'
        self.boku_seller_uuid = 'boku_seller_uuid'
        self.solitude.generic.product.get_object_or_404.return_value = {
            'resource_uri': '/generic/product/1/',
            'public_id': 'public_id',
            'seller_uuids': {
                'bango': self.bango_seller_uuid,
                'boku': self.boku_seller_uuid,
            },
            'seller': '/generic/seller/{s}/'.format(s=self.generic_seller_pk),
            'external_id': 'external_id',
        }
        seller = mock.Mock()
        self.solitude.generic.seller.return_value = seller
        seller.get_object_or_404.return_value = {
            'uuid': self.generic_seller_uuid
        }

    def start(self):
        prices = mock.Mock()
        prices.get_object.return_value = self.prices
        self.mkt.webpay.prices.return_value = prices
        self.solitude.get_transaction.return_value = {
            'status': constants.STATUS_CANCELLED,
            'notes': self.notes,
            'type': constants.TYPE_PAYMENT,
            'uuid': self.transaction_uuid
        }
        tasks.start_pay(self.transaction_uuid, self.notes, self.user_uuid,
                        [p.name for p in self.providers])

    def set_billing_id(self, slumber, num):
        slumber.bango.billing.post.return_value = {
            'resource_pk': '3333',
            'billingConfigurationId': num,
            'responseMessage': 'Success',
            'responseCode': 'OK',
            'resource_uri': '/bango/billing/3333/'
        }

    def test_product_queried_in_solitude_using_iss_for_in_app(self):
        public_id = 'inapp-public-id'
        self.notes['issuer_key'] = public_id
        self.start()
        self.solitude.generic.product.get_object_or_404.assert_any_call(
            public_id=public_id)

    def test_product_queried_in_solitude_using_public_id_for_web_app(self):
        public_id = 'webapp-public-id'
        self.notes['issuer_key'] = settings.KEY
        self.notes['pay_request']['request']['productData'] = (
            urlencode({'public_id': public_id}))
        self.start()
        self.solitude.generic.product.get_object_or_404.assert_any_call(
            public_id=public_id)

    def test_boku_used_when_network_supported_and_setup_in_solitude(self):
        mcc, mnc = api.BokuProvider.network_data.keys()[0]
        country = COUNTRIES[mcc]
        self.prices['prices'][0]['region'] = country
        self.notes['network'] = {
            'mcc': mcc,
            'mnc': mnc,
        }
        self.solitude.generic.seller.get_object_or_404.return_value = {
            'resource_pk': 1,
            'resource_uri': '/generic/seller/1/',
            'uuid': self.generic_seller_uuid,
        }
        self.providers = api.ProviderHelper.supported_providers(
            mcc=mcc, mnc=mnc)

        self.start()

        self.solitude.generic.seller.assert_called_with(
            self.generic_seller_pk)
        self.solitude.generic.seller.get_object_or_404.assert_called_with(
            uuid=self.generic_seller_uuid)
        self.solitude.boku.transactions.post.assert_called_with({
            'seller_uuid': self.boku_seller_uuid,
            'forward_url': 'http://testserver/mozpay/'
                           'provider/boku/wait-to-finish',
            'country': 'MX',
            'price': 1,
            'transaction_uuid': 'webpay:some-id',
            'callback_url': 'http://testserver/mozpay/'
                            'provider/boku/notification',
            'user_uuid': self.user_uuid,
        })

    def test_bango_used_when_boku_supported_but_not_setup_in_solitude(self):
        mcc, mnc = api.BokuProvider.network_data.keys()[0]
        country = COUNTRIES[mcc]
        self.prices['prices'][0]['region'] = country
        self.notes['network'] = {
            'mcc': mcc,
            'mnc': mnc,
        }
        self.solitude.generic.seller.get_object_or_404.return_value = {
            'resource_pk': 1,
            'uuid': self.boku_seller_uuid,
        }
        self.providers = api.ProviderHelper.supported_providers(
            mcc=mcc, mnc=mnc)

        self.solitude.generic.product.get_object_or_404.return_value = {
            'resource_uri': '/generic/product/1/',
            'public_id': 'public_id',
            'seller_uuids': {
                'bango': self.bango_seller_uuid,
                'boku': None,
            },
            'seller': '/generic/seller/1',
            'external_id': 'external_id',
        }

        self.start()

        self.solitude.bango.product.get_object_or_404.assert_called_with(
            seller_product__seller=1,
            seller_product__external_id='external_id'
        )

    def test_bango_used_when_boku_not_supported(self):
        self.providers = api.ProviderHelper.supported_providers()
        eq_(len(self.providers), 1)

        self.solitude.generic.seller.get_object_or_404.return_value = {
            'resource_pk': 1,
            'uuid': self.bango_seller_uuid,
        }

        self.solitude.generic.product.get_object_or_404.return_value = {
            'resource_uri': '/generic/product/1/',
            'public_id': 'public_id',
            'seller_uuids': {
                'bango': self.bango_seller_uuid,
                'boku': None,
            },
            'seller': '/generic/seller/1',
            'external_id': 'external_id',
        }

        self.start()

        self.solitude.bango.product.get_object_or_404.assert_called_with(
            seller_product__seller=1,
            seller_product__external_id='external_id'
        )

    @raises(api.SellerNotConfigured)
    def test_no_seller(self):
        raise SkipTest
        self.mkt.webpay.prices.return_value = self.prices
        self.solitude.generic.seller.get.return_value = {
            'meta': {'total_count': 0}
        }
        self.start()
        #eq_(self.get_trans().status, TRANS_STATE_FAILED)

    def test_transaction_called(self):
        self.solitude.generic.transaction.get_object.return_value = {
            'status': 'not-pending',
            'resource_pk': 5}
        self.start()
        self.solitude.generic.transaction.assert_called_with(5)

    def test_price_used(self):
        prices = mock.Mock()
        prices.get.return_value = self.prices
        self.mkt.webpay.prices.return_value = prices
        self.set_billing_id(self.solitude, 123)
        self.start()
        eq_(self.solitude.bango.billing.post.call_args[0][0]['prices'],
            self.prices['prices'])

    @mock.patch('webpay.pay.tasks.get_icon_url')
    def test_icon_url_sent(self, get_icon_url):
        url = 'http://mkt-cdn/media/icon.png'
        get_icon_url.return_value = url
        self.start()
        eq_(self.solitude.bango.billing.post.call_args[0][0]['icon_url'], url)

    def test_marketplace_other(self):
        self.start()
        eq_(self.solitude.bango.billing.post.call_args[0][0]['source'],
            'other')

    def test_marketplace_found(self):
        self.notes['pay_request']['request']['productData'] = (
            urlencode({'public_id': 'some-public-id'}))
        self.notes['issuer_key'] = settings.KEY
        self.start()
        eq_(self.solitude.bango.billing.post.call_args[0][0]['source'],
            'marketplace')

    @mock.patch('webpay.pay.tasks.get_icon_url')
    def test_icon_url_disabled(self, get_icon_url):
        with self.settings(USE_PRODUCT_ICONS=False):
            self.start()
        eq_(self.solitude.bango.billing.post.call_args[0][0]['icon_url'],
            None)
        assert not get_icon_url.called

    @mock.patch('webpay.pay.tasks.get_icon_url')
    def test_catch_icon_exceptions(self, get_icon_url):
        get_icon_url.side_effect = ValueError('just some exception')
        self.start()

    @mock.patch('webpay.pay.tasks.mkt_client.get_price')
    def test_price_fails(self, get_price):
        get_price.side_effect = UnknownPricePoint
        with self.assertRaises(UnknownPricePoint):
            self.start()

    @raises(RuntimeError)
    def test_exception_fails_transaction(self):
        raise SkipTest
        self.solitude.slumber.generic.seller.get.side_effect = RuntimeError
        self.start()
        #trans = self.get_trans()
        # Currently solitude doesn't have the concept of a failed transaction.
        # Perhaps we should add that?
        #eq_(trans.status, TRANS_STATE_FAILED)

    @mock.patch.object(settings, 'KEY', 'marketplace-domain')
    def test_pay_url_saved(self):
        bill_id = '123'
        self.mkt.webpay.prices.get.return_value = self.prices
        self.set_billing_id(self.solitude, bill_id)
        mock_trans = mock.Mock()
        self.solitude.generic.transaction.return_value = mock_trans

        self.start()

        assert mock_trans.patch.called
        post = mock_trans.patch.call_args[0][0]
        assert post['pay_url'].endswith('?bcid={b}'.format(b=bill_id)), (
            'Unexpected: {p}'.format(p=post['pay_url']))

    @mock.patch.object(settings, 'KEY', 'marketplace-domain')
    def test_marketplace_application_size(self):
        # Simulate how the Marketplace would add
        # a custom public_id and application_size
        # to the product data in the JWT.
        app_public_id = 'some-public-id'
        application_size = 10
        data = urlencode({
            'public_id': app_public_id,
            'application_size': application_size})
        self.notes['issuer_key'] = 'marketplace-domain'
        self.notes['pay_request']['request']['productData'] = data
        self.solitude.bango.product.get_object.side_effect = ObjectDoesNotExist
        self.start()

        # Check that the application size is the one submitted in productData.
        args = self.solitude.bango.billing.post.call_args
        eq_(args[0][0]['application_size'], application_size)

    @mock.patch.object(settings, 'KEY', 'marketplace-domain')
    def test_marketplace_wrong_application_size(self):
        app_public_id = 'some-public-id'
        application_size = 'foo'
        data = urlencode({
            'public_id': app_public_id,
            'application_size': application_size})
        self.notes['issuer_key'] = 'marketplace-domain'
        self.notes['pay_request']['request']['productData'] = data
        self.solitude.bango.product.get_object.side_effect = ObjectDoesNotExist
        self.start()

        # Check that the application size fallbacks to None if invalid.
        args = self.solitude.bango.billing.post.call_args
        eq_(args[0][0]['application_size'], None)

    @raises(ValueError)
    @mock.patch.object(settings, 'KEY', 'marketplace-domain')
    @mock.patch('webpay.pay.tasks.client')
    def test_marketplace_missing_public_id(self, cli):
        self.notes['issuer_key'] = settings.KEY
        self.notes['pay_request']['request']['productData'] = 'foo-bar'
        self.start()


class TestConfigureTransaction(BaseStartPay):

    def setUp(self):
        super(TestConfigureTransaction, self).setUp()

        p = mock.patch('webpay.pay.tasks.client')
        self.solitude = p.start()
        self.addCleanup(p.stop)

        p = mock.patch('lib.marketplace.api.client.api')
        self.mkt = p.start()
        self.addCleanup(p.stop)

        p = mock.patch('webpay.pay.tasks.start_pay.delay')
        self.start_pay = p.start()
        self.addCleanup(p.stop)

    @mock.patch('lib.solitude.api.client')
    @mock.patch('lib.marketplace.api.client.api')
    def start(self, marketplace, solitude, locale=None,
              session=None, request=None):
        if session is None:
            session = {}
        prices = mock.Mock()
        prices.get_object.return_value = self.prices
        marketplace.webpay.prices.return_value = prices
        solitude.get_transaction.return_value = {
            'status': constants.STATUS_CANCELLED,
            'notes': self.notes,
            'type': constants.TYPE_PAYMENT,
            'uuid': self.transaction_uuid
        }
        if request is None:
            request = RequestFactory().get('/')
            if locale:
                request.locale = locale
            request.session = session
            request.session['trans_id'] = self.transaction_uuid
            request.session['notes'] = self.notes
            request.session['is_simulation'] = False
            request.session['uuid'] = self.user_uuid
        return tasks.configure_transaction(request)

    def test_prevent_reconfiguring_transaction(self):
        self.solitude.side_effect = ObjectDoesNotExist
        session = {}
        ok_(self.start(session=session))
        ok_(not self.start(session=session))  # Second call should do nothing.
        eq_(self.start_pay.call_count, 1)

    def test_no_trans_id(self):
        request = RequestFactory().get('/')
        request.session = {}
        eq_(self.start(request=request), False)

    def test_restart_certain_transactions(self):
        for st in constants.STATUS_RETRY_OK:
            self.solitude.get_transaction.return_value = {
                'status': st, 'resource_pk': '1',
                'notes': {}
            }
            ok_(self.start())
            assert self.start_pay.called, (
                'Expected start_pay for status %s' % st)
            assert self.start_pay.call_args[0][0] != self.transaction_uuid, (
                'Expected a new transaction ID')

    def test_retry_trans_even_when_configured(self):
        self.solitude.get_transaction.return_value = {
            # Failed transactions are ok to retry.
            'status': constants.STATUS_FAILED, 'resource_pk': '1',
            'notest': {}
        }
        session = {}
        ok_(self.start(session=session))
        ok_(self.start(session=session))  # Second call should still configure.
        eq_(self.start_pay.call_count, 2)

    def test_use_locale_name(self):
        name = 'Die App Հ'
        description = 'Die beste Beschreibung.'
        self.notes['pay_request']['request']['locales'] = {
            'de': {
                'name': name,
                'description': description
            }
        }
        self.solitude.get_transaction.return_value = {
            'status': constants.STATUS_CANCELLED, 'resource_pk': '1',
            'notes': self.notes
        }
        ok_(self.start(locale='de'))
        start_pay = self.start_pay
        assert start_pay.called
        eq_(start_pay.call_args[0][1]['pay_request']['request']['name'],
            name)
        eq_(start_pay.call_args[0][1]['pay_request']['request']['description'],
            description)


class TestLocalizePayRequest(test_utils.TestCase):

    def setUp(self):
        self.request = RequestFactory().get('/')
        self.request.session = {
            'notes': {
                'pay_request': {
                    'request': {
                        'name': 'Virtual Sword',
                        'description': 'A fancy sword',
                        'locales': {
                            'en': {
                                'name': 'English Virtual Sword',
                                'description': 'A fancy English sword'
                            },
                            'en-GB': {
                                'name': 'British Virtual Sword'
                            }
                        }
                    }
                }
            }
        }

    def _get_pay_request_details(self):
        return self.request.session['notes']['pay_request']['request']

    def test_no_locale(self):
        tasks._localize_pay_request(self.request)
        req = self._get_pay_request_details()
        eq_(req['name'], 'Virtual Sword')
        eq_(req['description'], 'A fancy sword')

    def test_only_lang(self):
        self.request.locale = 'en'
        tasks._localize_pay_request(self.request)
        req = self._get_pay_request_details()
        eq_(req['name'], 'English Virtual Sword')
        eq_(req['description'], 'A fancy English sword')

    def test_lang_and_region(self):
        self.request.locale = 'en-GB'
        tasks._localize_pay_request(self.request)
        req = self._get_pay_request_details()
        eq_(req['name'], 'British Virtual Sword')
        eq_(req['description'], 'A fancy sword')

    def test_lang_and_unlocalized_region(self):
        self.request.locale = 'en-US'
        tasks._localize_pay_request(self.request)
        req = self._get_pay_request_details()
        eq_(req['name'], 'English Virtual Sword')
        eq_(req['description'], 'A fancy English sword')


class TestGetIconURL(test_utils.TestCase):

    def setUp(self):
        p = mock.patch('lib.marketplace.api.client.api')
        self.marketplace = p.start()
        self.addCleanup(p.stop)
        self.request = {'icons': {'64': 'http://app/icon.png'}}
        self.size = 64
        p = mock.patch.object(settings, 'PRODUCT_ICON_SIZE', self.size)
        p.start()
        self.addCleanup(p.stop)

    def get_icon_url(self):
        return tasks.get_icon_url(self.request)

    def test_get_url_from_api(self):
        url = 'http://mkt-cdn/media/icon.png'
        icon = {'url': url}
        self.marketplace.webpay.product.icon.get_object.return_value = icon
        eq_(self.get_icon_url(), url)

    def test_no_cached_icon(self):
        icon = self.marketplace.webpay.product.icon
        icon.get_object.side_effect = ObjectDoesNotExist()
        eq_(self.get_icon_url(), None)
        post = self.marketplace.webpay.product.icon.post
        post.assert_called_with(dict(ext_url=self.request['icons']['64'],
                                     size=64, ext_size=64))

    def test_no_app_icons(self):
        del self.request['icons']
        eq_(self.get_icon_url(), None)

    def test_empty_app_icons(self):
        self.request['icons'] = {}
        eq_(self.get_icon_url(), None)

    def test_use_largest(self):
        self.request = {'icons': {'128': 'http://app/128.png',
                                  '512': 'http://app/512.png'}}
        self.get_icon_url()
        get = self.marketplace.webpay.product.icon.get_object
        get.assert_called_with(ext_url=self.request['icons']['512'],
                               size=self.size, ext_size='512')

    def test_use_exact(self):
        self.request = {'icons': {'64': 'http://app/64.png',
                                  '512': 'http://app/512.png'}}
        self.get_icon_url()
        get = self.marketplace.webpay.product.icon.get_object
        get.assert_called_with(ext_url=self.request['icons']['64'],
                               size=64, ext_size=64)

    def test_icon_too_small_to_resize(self):
        self.request = {'icons': {'48': 'http://app/48.png'}}
        self.get_icon_url()
        get = self.marketplace.webpay.product.icon.get_object
        get.assert_called_with(ext_url=self.request['icons']['48'],
                               size='48', ext_size='48')


class TestConfigureTrans(test.TestCase):

    @mock.patch('lib.solitude.api.client.get_transaction')
    def configure(self, get_trans):
        get_trans.side_effect = ObjectDoesNotExist
        return tasks.configure_transaction(self.request)

    def setUp(self):
        self.request = mock.Mock()
        sess = {}
        self.request.session = sess
        sess['is_simulation'] = False
        sess['notes'] = {}
        sess['trans_id'] = 'trans-id'
        sess['uuid'] = 'some-email-token'
        patch = mock.patch('webpay.pay.tasks.start_pay')
        self.start_pay = patch.start()
        self.addCleanup(patch.stop)

    def test_configure(self):
        ok_(self.configure())
        assert self.start_pay.delay.called
        assert 'notes' in self.request.session, (
            'notes should remain in the session after '
            'passing it to start_pay so that super_simulate() works')

    def test_skip_when_simulating(self):
        self.request.session['is_simulation'] = True
        self.configure()
        assert not self.start_pay.delay.called
