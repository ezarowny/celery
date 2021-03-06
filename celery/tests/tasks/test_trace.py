from __future__ import absolute_import

from kombu.exceptions import EncodeError

from celery import group, uuid
from celery import signals
from celery import states
from celery.exceptions import Ignore, Retry, Reject
from celery.app.trace import (
    TraceInfo,
    build_tracer,
    get_log_policy,
    log_policy_reject,
    log_policy_ignore,
    log_policy_internal,
    log_policy_expected,
    log_policy_unexpected,
    trace_task,
    _trace_task_ret,
    _fast_trace_task,
    setup_worker_optimizations,
    reset_worker_optimizations,
)
from celery.tests.case import AppCase, Mock, patch


def trace(app, task, args=(), kwargs={},
          propagate=False, eager=True, request=None, **opts):
    t = build_tracer(task.name, task,
                     eager=eager, propagate=propagate, app=app, **opts)
    ret = t('id-1', args, kwargs, request)
    return ret.retval, ret.info


class TraceCase(AppCase):

    def setup(self):
        @self.app.task(shared=False)
        def add(x, y):
            return x + y
        self.add = add

        @self.app.task(shared=False, ignore_result=True)
        def add_cast(x, y):
            return x + y
        self.add_cast = add_cast

        @self.app.task(shared=False)
        def raises(exc):
            raise exc
        self.raises = raises

    def trace(self, *args, **kwargs):
        return trace(self.app, *args, **kwargs)


class test_trace(TraceCase):

    def test_trace_successful(self):
        retval, info = self.trace(self.add, (2, 2), {})
        self.assertIsNone(info)
        self.assertEqual(retval, 4)

    def test_trace_on_success(self):

        @self.app.task(shared=False, on_success=Mock())
        def add_with_success(x, y):
            return x + y

        self.trace(add_with_success, (2, 2), {})
        self.assertTrue(add_with_success.on_success.called)

    def test_get_log_policy(self):
        einfo = Mock(name='einfo')
        einfo.internal = False
        self.assertIs(
            get_log_policy(self.add, einfo, Reject()),
            log_policy_reject,
        )
        self.assertIs(
            get_log_policy(self.add, einfo, Ignore()),
            log_policy_ignore,
        )
        self.add.throws = (TypeError,)
        self.assertIs(
            get_log_policy(self.add, einfo, KeyError()),
            log_policy_unexpected,
        )
        self.assertIs(
            get_log_policy(self.add, einfo, TypeError()),
            log_policy_expected,
        )
        einfo2 = Mock(name='einfo2')
        einfo2.internal = True
        self.assertIs(
            get_log_policy(self.add, einfo2, KeyError()),
            log_policy_internal,
        )

    def test_trace_after_return(self):

        @self.app.task(shared=False, after_return=Mock())
        def add_with_after_return(x, y):
            return x + y

        self.trace(add_with_after_return, (2, 2), {})
        self.assertTrue(add_with_after_return.after_return.called)

    def test_with_prerun_receivers(self):
        on_prerun = Mock()
        signals.task_prerun.connect(on_prerun)
        try:
            self.trace(self.add, (2, 2), {})
            self.assertTrue(on_prerun.called)
        finally:
            signals.task_prerun.receivers[:] = []

    def test_with_postrun_receivers(self):
        on_postrun = Mock()
        signals.task_postrun.connect(on_postrun)
        try:
            self.trace(self.add, (2, 2), {})
            self.assertTrue(on_postrun.called)
        finally:
            signals.task_postrun.receivers[:] = []

    def test_with_success_receivers(self):
        on_success = Mock()
        signals.task_success.connect(on_success)
        try:
            self.trace(self.add, (2, 2), {})
            self.assertTrue(on_success.called)
        finally:
            signals.task_success.receivers[:] = []

    def test_when_chord_part(self):

        @self.app.task(shared=False)
        def add(x, y):
            return x + y
        add.backend = Mock()

        request = {'chord': uuid()}
        self.trace(add, (2, 2), {}, request=request)
        self.assertTrue(add.backend.mark_as_done.called)
        args, kwargs = add.backend.mark_as_done.call_args
        self.assertEqual(args[0], 'id-1')
        self.assertEqual(args[1], 4)
        self.assertEqual(args[2].chord, request['chord'])
        self.assertFalse(args[3])

    def test_when_backend_cleanup_raises(self):

        @self.app.task(shared=False)
        def add(x, y):
            return x + y
        add.backend = Mock(name='backend')
        add.backend.process_cleanup.side_effect = KeyError()
        self.trace(add, (2, 2), {}, eager=False)
        add.backend.process_cleanup.assert_called_with()
        add.backend.process_cleanup.side_effect = MemoryError()
        with self.assertRaises(MemoryError):
            self.trace(add, (2, 2), {}, eager=False)

    def test_when_Ignore(self):

        @self.app.task(shared=False)
        def ignored():
            raise Ignore()

        retval, info = self.trace(ignored, (), {})
        self.assertEqual(info.state, states.IGNORED)

    def test_when_Reject(self):

        @self.app.task(shared=False)
        def rejecting():
            raise Reject()

        retval, info = self.trace(rejecting, (), {})
        self.assertEqual(info.state, states.REJECTED)

    def test_backend_cleanup_raises(self):
        self.add.backend.process_cleanup = Mock()
        self.add.backend.process_cleanup.side_effect = RuntimeError()
        self.trace(self.add, (2, 2), {})

    @patch('celery.canvas.maybe_signature')
    def test_callbacks__scalar(self, maybe_signature):
        sig = Mock(name='sig')
        request = {'callbacks': [sig], 'root_id': 'root'}
        maybe_signature.return_value = sig
        retval, _ = self.trace(self.add, (2, 2), {}, request=request)
        sig.apply_async.assert_called_with(
            (4,), parent_id='id-1', root_id='root',
        )

    @patch('celery.canvas.maybe_signature')
    def test_chain_proto2(self, maybe_signature):
        sig = Mock(name='sig')
        sig2 = Mock(name='sig2')
        request = {'chain': [sig2, sig], 'root_id': 'root'}
        maybe_signature.return_value = sig
        retval, _ = self.trace(self.add, (2, 2), {}, request=request)
        sig.apply_async.assert_called_with(
            (4, ), parent_id='id-1', root_id='root',
            chain=[sig2],
        )

    @patch('celery.canvas.maybe_signature')
    def test_callbacks__EncodeError(self, maybe_signature):
        sig = Mock(name='sig')
        request = {'callbacks': [sig], 'root_id': 'root'}
        maybe_signature.return_value = sig
        sig.apply_async.side_effect = EncodeError()
        retval, einfo = self.trace(self.add, (2, 2), {}, request=request)
        self.assertEqual(einfo.state, states.FAILURE)

    @patch('celery.canvas.maybe_signature')
    @patch('celery.app.trace.group.apply_async')
    def test_callbacks__sigs(self, group_, maybe_signature):
        sig1 = Mock(name='sig')
        sig2 = Mock(name='sig2')
        sig3 = group([Mock(name='g1'), Mock(name='g2')], app=self.app)
        sig3.apply_async = Mock(name='gapply')
        request = {'callbacks': [sig1, sig3, sig2], 'root_id': 'root'}

        def passt(s, *args, **kwargs):
            return s
        maybe_signature.side_effect = passt
        retval, _ = self.trace(self.add, (2, 2), {}, request=request)
        group_.assert_called_with(
            (4,), parent_id='id-1', root_id='root',
        )
        sig3.apply_async.assert_called_with(
            (4,), parent_id='id-1', root_id='root',
        )

    @patch('celery.canvas.maybe_signature')
    @patch('celery.app.trace.group.apply_async')
    def test_callbacks__only_groups(self, group_, maybe_signature):
        sig1 = group([Mock(name='g1'), Mock(name='g2')], app=self.app)
        sig2 = group([Mock(name='g3'), Mock(name='g4')], app=self.app)
        sig1.apply_async = Mock(name='gapply')
        sig2.apply_async = Mock(name='gapply')
        request = {'callbacks': [sig1, sig2], 'root_id': 'root'}

        def passt(s, *args, **kwargs):
            return s
        maybe_signature.side_effect = passt
        retval, _ = self.trace(self.add, (2, 2), {}, request=request)
        sig1.apply_async.assert_called_with(
            (4,), parent_id='id-1', root_id='root',
        )
        sig2.apply_async.assert_called_with(
            (4,), parent_id='id-1', root_id='root',
        )

    def test_trace_SystemExit(self):
        with self.assertRaises(SystemExit):
            self.trace(self.raises, (SystemExit(),), {})

    def test_trace_Retry(self):
        exc = Retry('foo', 'bar')
        _, info = self.trace(self.raises, (exc,), {})
        self.assertEqual(info.state, states.RETRY)
        self.assertIs(info.retval, exc)

    def test_trace_exception(self):
        exc = KeyError('foo')
        _, info = self.trace(self.raises, (exc,), {})
        self.assertEqual(info.state, states.FAILURE)
        self.assertIs(info.retval, exc)

    def test_trace_task_ret__no_content_type(self):
        _trace_task_ret(
            self.add.name, 'id1', {}, ((2, 2), {}), None, None,
            app=self.app,
        )

    def test_fast_trace_task__no_content_type(self):
        self.app.tasks[self.add.name].__trace__ = build_tracer(
            self.add.name, self.add, app=self.app,
        )
        _fast_trace_task(
            self.add.name, 'id1', {}, ((2, 2), {}), None, None,
            app=self.app, _loc=[self.app.tasks, {}, 'hostname']
        )

    def test_trace_exception_propagate(self):
        with self.assertRaises(KeyError):
            self.trace(self.raises, (KeyError('foo'),), {}, propagate=True)

    @patch('celery.app.trace.build_tracer')
    @patch('celery.app.trace.report_internal_error')
    def test_outside_body_error(self, report_internal_error, build_tracer):
        tracer = Mock()
        tracer.side_effect = KeyError('foo')
        build_tracer.return_value = tracer

        @self.app.task(shared=False)
        def xtask():
            pass

        trace_task(xtask, 'uuid', (), {})
        self.assertTrue(report_internal_error.call_count)
        self.assertIs(xtask.__trace__, tracer)


class test_TraceInfo(TraceCase):

    class TI(TraceInfo):
        __slots__ = TraceInfo.__slots__ + ('__dict__',)

    def test_handle_error_state(self):
        x = self.TI(states.FAILURE)
        x.handle_failure = Mock()
        x.handle_error_state(self.add_cast, self.add_cast.request)
        x.handle_failure.assert_called_with(
            self.add_cast, self.add_cast.request,
            store_errors=self.add_cast.store_errors_even_if_ignored,
        )

    @patch('celery.app.trace.ExceptionInfo')
    def test_handle_reject(self, ExceptionInfo):
        x = self.TI(states.FAILURE)
        x._log_error = Mock(name='log_error')
        req = Mock(name='req')
        x.handle_reject(self.add, req)
        x._log_error.assert_called_with(self.add, req, ExceptionInfo())


class test_stackprotection(AppCase):

    def test_stackprotection(self):
        setup_worker_optimizations(self.app)
        try:
            @self.app.task(shared=False, bind=True)
            def foo(self, i):
                if i:
                    return foo(0)
                return self.request

            self.assertTrue(foo(1).called_directly)
        finally:
            reset_worker_optimizations()
