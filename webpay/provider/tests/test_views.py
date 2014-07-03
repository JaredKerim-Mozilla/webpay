import json

from django.conf import settings
from django.core.exceptions import ObjectDoesNotExist
from django.core.urlresolvers import reverse

import mock
from nose.tools import eq_, raises
from slumber.exceptions import HttpClientError

from lib.solitude.constants import (
    STATUS_CANCELLED, STATUS_COMPLETED, STATUS_FAILED, STATUS_PENDING)
from webpay.base import dev_messages as msg
from webpay.base.tests import BasicSessionCase


class ProviderTestCase(BasicSessionCase):

    def setUp(self):
        super(ProviderTestCase, self).setUp()

        # Log in.
        self.buyer_uuid = 'verified-user'
        self.session['uuid'] = self.buyer_uuid
        # Start a payment.
        self.trans_id = 'solitude-trans-uuid'
        self.session['trans_id'] = self.trans_id
        self.session['notes'] = {'pay_request': '<request>',
                                 'issuer_key': '<issuer>'}
        self.save_session()

        # TODO: Add this when verifying tokens. bug 936138
        p = mock.patch('lib.solitude.api.client.slumber')
        self.slumber = p.start()
        self.addCleanup(p.stop)

        p = mock.patch('webpay.provider.views.tasks.payment_notify')
        self.payment_notify = p.start()
        self.addCleanup(p.stop)

        p = mock.patch('webpay.provider.views.tasks.chargeback_notify')
        self.chargeback_notify = p.start()
        self.addCleanup(p.stop)


@mock.patch.object(settings, 'PAYMENT_PROVIDER', 'reference')
class TestProviderSuccess(ProviderTestCase):

    def trust_notice(self):
        post = self.slumber.provider.reference.notices.post
        post.return_value = {'result': 'OK'}

    def callback(self, data=None, clear_qs=False, view=None):
        assert view, 'not sure which view to use'
        params = {'ext_transaction_id': self.trans_id}
        if clear_qs:
            params.clear()
        if data:
            params.update(data)
        return self.client.get(view, params)

    def error(self, **kw):
        kw['view'] = reverse('provider.error', args=['reference'])
        return self.callback(**kw)

    def success(self, **kw):
        kw['view'] = reverse('provider.success', args=['reference'])
        return self.callback(**kw)

    def test_missing_ext_trans(self):
        self.trust_notice()
        res = self.success(clear_qs=True)
        self.assertContains(res, msg.TRANS_MISSING, status_code=400)

    def test_missing_session_trans(self):
        self.trust_notice()
        del self.session['trans_id']
        self.save_session()
        res = self.success()
        self.assertContains(res, msg.NO_ACTIVE_TRANS,
                            status_code=400)

    def test_success(self):
        self.trust_notice()
        res = self.success()
        eq_(res.status_code, 200)
        self.payment_notify.delay.assert_called_with(self.trans_id,
                                                     self.buyer_uuid)
        self.assertTemplateUsed(res, 'provider/success.html')

    def test_error(self):
        self.trust_notice()
        res = self.error()
        eq_(res.status_code, 400)
        assert not self.payment_notify.delay.called, (
            'did not expect a notification on error')
        self.assertTemplateUsed(res, 'error.html')

    def test_invalid_notice_on_success(self):
        post = self.slumber.provider.reference.notices.post
        post.return_value = {'result': 'FAIL'}

        res = self.success()
        eq_(res.status_code, 400)

    def test_notice_failure(self):
        post = self.slumber.provider.reference.notices.post
        post.side_effect = HttpClientError('bad stuff')

        res = self.success()
        eq_(res.status_code, 400)

    def test_invalid_notice_on_error(self):
        post = self.slumber.provider.reference.notices.post
        post.return_value = {'result': 'FAIL'}

        res = self.error()
        eq_(res.status_code, 400)


class TestWaitToFinish(ProviderTestCase):

    def setUp(self):
        super(TestWaitToFinish, self).setUp()
        self.wait_url = reverse('provider.wait_to_finish', args=['boku'])

    def wait(self, trans_uuid=None):
        if not trans_uuid:
            trans_uuid = self.trans_id
        return self.client.get('{u}?param={t}'.format(u=self.wait_url,
                                                      t=trans_uuid))

    def test_wait_for_boku_transaction(self):
        res = self.wait()
        url = reverse('provider.transaction_status', args=[self.trans_id])
        eq_(res.context['transaction_status_url'], url)
        eq_(res.status_code, 200)

    def test_missing_transaction(self):
        res = self.client.get('{u}?foo=bar'.format(u=self.wait_url))
        eq_(res.status_code, 404)


class TestTransactionStatus(ProviderTestCase):

    def status(self, trans_id=None):
        if not trans_id:
            trans_id = self.trans_id
        return self.client.get(reverse('provider.transaction_status',
                                       args=[trans_id]))

    def test_not_in_session(self):
        res = self.status(trans_id='not-my-transaction')
        eq_(res.status_code, 403)

    def test_pending(self):
        self.slumber.generic.transaction.get_object.return_value = {
            'uuid': self.trans_id,
            'notes': '{}',
            'status': STATUS_PENDING}
        res = self.status()
        eq_(res.status_code, 203)
        eq_(json.loads(res.content), {'status': STATUS_PENDING, 'url': None})
        self.slumber.generic.transaction.get_object.assert_called_with(
            uuid=self.trans_id)

    def test_not_found(self):
        get = self.slumber.generic.transaction.get_object
        get.side_effect = ObjectDoesNotExist
        res = self.status()
        eq_(res.status_code, 404)


class TestNotification(ProviderTestCase):

    def setUp(self):
        super(TestNotification, self).setUp()
        self.buyer_uuid = 'buyer_uuid'
        self.session['uuid'] = self.buyer_uuid
        self.save_session()

        self.url = reverse('provider.notification', args=['boku'])
        self.slumber.generic.transaction.get_object.return_value = {
            'uuid': self.trans_id,
            'notes': '{}',
            'status': STATUS_COMPLETED}

    def notify(self):
        return self.client.get('{url}?param={tr}'.format(url=self.url,
                                                         tr=self.trans_id))

    def test_completed_transaction(self):
        res = self.notify()
        eq_(res.status_code, 200)
        self.slumber.provider.boku.event.post.assert_called_with(
            {'param': [self.trans_id]})
        self.payment_notify.delay.assert_called_with(self.trans_id,
                                                     self.buyer_uuid)

    @raises(NotImplementedError)
    def test_not_implemented(self):
        self.url = reverse('provider.notification', args=['bango'])
        self.notify()

    def test_api_failure(self):
        self.slumber.provider.boku.event.post.side_effect = HttpClientError
        res = self.notify()
        eq_(res.status_code, 502)
        eq_(res.content, 'NOTICE_ERROR')
        assert not self.payment_notify.delay.called, (
            'A notification should not be sent on failure')

    def test_failed_transaction(self):
        self.slumber.generic.transaction.get_object.return_value = {
            'uuid': self.trans_id,
            'notes': '{}',
            'status': STATUS_FAILED}
        res = self.notify()
        assert not self.payment_notify.delay.called, (
            'A notification should not be sent on failed transactions')
        eq_(res.status_code, 200)

    def test_cancelled_transaction(self):
        self.slumber.generic.transaction.get_object.return_value = {
            'uuid': self.trans_id,
            'notes': '{}',
            'status': STATUS_CANCELLED}
        res = self.notify()
        assert not self.payment_notify.delay.called, (
            'A notification should not be sent on cancelled transactions')
        assert not self.chargeback_notify.delay.called, (
            'Chargeback notices should not be sent on cancelled transactions')
        eq_(res.status_code, 200)
