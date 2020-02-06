from __future__ import absolute_import
import os
import re
import logging
import json
from json import JSONEncoder
import signal
import time
from multiprocessing import Process, Value, current_process
import atexit
import datetime
import collections
from pathlib import Path

import enum
from enum import Enum
import zmq
import dateutil.parser

from sdcm.utils.common import safe_kill, pid_exists, makedirs, retrying, timeout

LOGGER = logging.getLogger(__name__)


class EventsDevice(Process):

    def __init__(self, log_dir):
        super(EventsDevice, self).__init__()
        self.pub_port = Value('d', 0)
        self.sub_port = Value('d', 0)

        self.event_log_base_dir = os.path.join(log_dir, 'events_log')
        makedirs(self.event_log_base_dir)
        self.raw_events_filename = os.path.join(self.event_log_base_dir, 'raw_events.log')
        self._client_socket = None

    def run(self):
        try:
            context = zmq.Context(1)
            # Socket facing clients
            frontend = context.socket(zmq.SUB)  # pylint: disable=no-member
            self.pub_port.value = frontend.bind_to_random_port("tcp://*")
            frontend.setsockopt(zmq.SUBSCRIBE, b"")  # pylint: disable=no-member

            # Socket facing services
            backend = context.socket(zmq.PUB)  # pylint: disable=no-member
            self.sub_port.value = backend.bind_to_random_port("tcp://*")
            LOGGER.info("EventDevice Listen on pub_port=%d, sub_port=%d", self.pub_port.value, self.sub_port.value)

            backend.setsockopt(zmq.LINGER, 0)  # pylint: disable=no-member
            frontend.setsockopt(zmq.LINGER, 0)  # pylint: disable=no-member
            zmq.proxy(frontend, backend)  # pylint: disable=no-member

        except Exception:  # pylint: disable=broad-except
            LOGGER.exception("zmq device failed")
        except (KeyboardInterrupt, SystemExit) as ex:
            LOGGER.debug("EventsDevice was halted by %s", ex.__class__.__name__)
        finally:
            frontend.close()
            backend.close()
            context.term()

    @staticmethod
    @timeout(timeout=120)
    def wait_till_event_loop_is_working(number_of_events):
        """
        It waits 120 seconds till row of {number_of_events} events is delivered with no loss
        """
        for _ in range(number_of_events):
            try:
                StartupTestEvent().publish(guaranteed=True)
            except TimeoutError:
                raise RuntimeError("Event loop is not working properly")

    def get_client_socket(self, filter_type=b'', reuse_socket=False):
        if reuse_socket:
            if self._client_socket is not None:
                return self._client_socket
        context = zmq.Context()
        socket = context.socket(zmq.SUB)  # pylint: disable=no-member
        socket.connect("tcp://localhost:%d" % self.sub_port.value)
        socket.setsockopt(zmq.SUBSCRIBE, filter_type)  # pylint: disable=no-member
        if reuse_socket:
            self._client_socket = socket
        return socket

    def subscribe_events(self, filter_type=b'', stop_event=None):
        # pylint: disable=too-many-nested-blocks,too-many-branches
        LOGGER.info("subscribe to server with port %d", self.sub_port.value)
        socket = self.get_client_socket(filter_type)
        filters = dict()
        try:
            while stop_event is None or not stop_event.isSet():
                if socket.poll(timeout=1):
                    obj = socket.recv_pyobj()

                    # remove filter objects when log event timestamp on the
                    # specific node is bigger the time filter was canceled
                    if isinstance(obj, DatabaseLogEvent):
                        for filter_key, filter_obj in list(filters.items()):
                            if filter_obj.expire_time and filter_obj.expire_time < obj.timestamp:
                                del filters[filter_key]

                    obj_filtered = any([f.eval_filter(obj) for f in filters.values()])

                    if isinstance(obj, DbEventsFilter):
                        if not obj.clear_filter:
                            filters[obj.id] = obj
                        else:
                            object_filter = filters.get(obj.id, None)
                            if object_filter is None:
                                filters[obj.id] = obj
                            else:
                                filters[obj.id].expire_time = obj.expire_time
                            if not obj.expire_time:
                                del filters[obj.id]

                    obj_filtered = obj_filtered or isinstance(obj, SystemEvent)
                    if not obj_filtered:

                        yield obj.__class__.__name__, obj
        except (KeyboardInterrupt, SystemExit) as ex:
            LOGGER.debug("%s - subscribe_events was halted by %s", current_process().name, ex.__class__.__name__)
        socket.close()

    def publish_event(self, event):
        context = zmq.Context()
        socket = context.socket(zmq.PUB)  # pylint: disable=no-member
        socket.connect("tcp://localhost:%d" % self.pub_port.value)
        time.sleep(0.01)

        socket.send_pyobj(event)
        with open(self.raw_events_filename, 'a+') as log_file:
            log_file.write(event.to_json() + '\n')
        socket.close()
        return True

    @retrying(n=3, sleep_time=0, allowed_exceptions=TimeoutError)
    def publish_event_guaranteed(self, event, reuse_socket=True):
        client_socket = self.get_client_socket(reuse_socket=reuse_socket)
        self.publish_event(event)
        if not client_socket.poll(timeout=1):
            raise TimeoutError()
        received_event = client_socket.recv_pyobj()
        if not reuse_socket:
            client_socket.close()
        if event == received_event:
            return True
        raise TimeoutError()


# monkey patch JSONEncoder make enums jsonable
_SAVED_DEFAULT = JSONEncoder().default  # Save default method.


def _new_default(self, obj):  # pylint: disable=unused-argument
    if isinstance(obj, Enum):
        return obj.name  # Could also be obj.value
    else:
        return _SAVED_DEFAULT


JSONEncoder.default = _new_default  # Set new default method.


class Severity(enum.Enum):
    NORMAL = 1
    WARNING = 2
    ERROR = 3
    CRITICAL = 4


class SctEvent():
    def __init__(self):
        self.timestamp = time.time()
        self.severity = Severity.NORMAL

    @property
    def formatted_timestamp(self):
        try:
            return datetime.datetime.fromtimestamp(self.timestamp).strftime("%Y-%m-%d %H:%M:%S.%f")[:-3]
        except ValueError:
            LOGGER.exception("failed to format timestamp:[%d]", self.timestamp)
            return '<UnknownTimestamp>'

    def publish(self, guaranteed=True):
        if guaranteed:
            return EVENTS_PROCESSES['MainDevice'].publish_event_guaranteed(self)
        return EVENTS_PROCESSES['MainDevice'].publish_event(self)

    def __str__(self):
        return "({} {})".format(self.__class__.__name__, self.severity)

    def to_json(self):
        return json.dumps(self.__dict__)

    def __eq__(self, other):
        if not isinstance(other, self.__class__):
            return False
        return self.__dict__ == other.__dict__


class SystemEvent(SctEvent):
    pass


class StartupTestEvent(SystemEvent):
    def __init__(self):
        super().__init__()
        self.severity = Severity.NORMAL


class TestFrameworkEvent(SctEvent):  # pylint: disable=too-many-instance-attributes
    def __init__(self, source, source_method,  # pylint: disable=redefined-builtin,too-many-arguments
                 exception=None, message=None, args=None, kwargs=None, severity=None):
        super().__init__()
        if severity is None:
            self.severity = Severity.CRITICAL
        else:
            self.severity = severity
        self.source = str(source)
        self.source_method = str(source_method)
        self.exception = str(exception)
        self.message = str(message)
        self.args = args
        self.kwargs = kwargs

    def __str__(self):
        message = f'message={self.message}' if self.message else ''
        message += f'\nexception={self.exception}' if self.exception else ''
        args = f' args={self.args}' if self.args else ''
        kwargs = f' kwargs={self.kwargs}' if self.kwargs else ''
        params = ','.join([args, kwargs]) if kwargs or args else ''
        return f"{super().__str__()}, source={self.source}.{self.source_method}({params}){message}"


class DbEventsFilter(SystemEvent):
    def __init__(self, type, line=None, node=None):  # pylint: disable=redefined-builtin
        super(DbEventsFilter, self).__init__()
        self.id = id(self)  # pylint: disable=invalid-name
        self.type = type
        self.line = line
        self.node = str(node) if node else None
        self.clear_filter = False
        self.expire_time = None
        self.publish()

    def __eq__(self, other):
        if not isinstance(other, self.__class__):
            return False
        return self.id == other.id

    def cancel_filter(self):
        self.clear_filter = True
        if self.node:
            self.expire_time = time.time()
        self.publish()

    def __enter__(self):
        return self

    def __exit__(self, exception_type, exception_value, traceback):
        self.cancel_filter()

    def eval_filter(self, event):
        line = getattr(event, 'line', '')
        _type = getattr(event, 'type', '')
        node = getattr(event, 'node', '')
        is_name_matching = self.type and _type and self.type == _type
        is_line_matching = self.line and line and self.line in line
        is_node_matching = self.node and node and self.node == node

        result = is_name_matching
        if self.line:
            result = result and is_line_matching
        if self.node:
            result = result and is_node_matching
        return result


class InfoEvent(SctEvent):
    def __init__(self, message):
        super(InfoEvent, self).__init__()
        self.message = message
        self.severity = Severity.NORMAL
        self.publish()

    def __str__(self):
        return "{0}: message={1.message}".format(super(InfoEvent, self).__str__(), self)


class ThreadFailedEvent(SctEvent):
    def __init__(self, message, traceback):
        super(ThreadFailedEvent, self).__init__()
        self.message = message
        self.severity = Severity.ERROR
        self.traceback = str(traceback)
        self.publish()

    def __str__(self):
        return f"{super().__str__()}: message={self.message}\n{self.traceback}"


class CoreDumpEvent(SctEvent):
    def __init__(self, corefile_url, download_instructions,  # pylint: disable=too-many-arguments
                 backtrace, node, timestamp=None):
        super(CoreDumpEvent, self).__init__()
        self.corefile_url = corefile_url
        self.download_instructions = download_instructions
        self.backtrace = backtrace
        self.severity = Severity.CRITICAL
        self.node = str(node)
        if timestamp is not None:
            self.timestamp = timestamp
        self.publish()

    def __str__(self):
        return "{0}: node={1.node}\ncorefile_url=\n{1.corefile_url}\nbacktrace={1.backtrace}\ndownload_instructions=\n{1.download_instructions}".format(
            super(CoreDumpEvent, self).__str__(), self)


class KillTestEvent(SctEvent):
    def __init__(self, reason):
        super(KillTestEvent, self).__init__()
        self.reason = reason
        self.severity = Severity.CRITICAL
        self.publish()

    def __str__(self):
        return "{0}: reason={1.reason}".format(super(KillTestEvent, self).__str__(), self)


class DisruptionEvent(SctEvent):  # pylint: disable=too-many-instance-attributes
    def __init__(self, type, name, status, start=None, end=None, duration=None, node=None, error=None, full_traceback=None, **kwargs):  # pylint: disable=redefined-builtin,too-many-arguments
        super(DisruptionEvent, self).__init__()
        self.name = name
        self.type = type
        self.start = start
        self.end = end
        self.duration = duration
        self.node = str(node)
        self.severity = Severity.NORMAL if status else Severity.ERROR
        self.error = None
        self.full_traceback = ''
        if error:
            self.error = error
            self.full_traceback = str(full_traceback)

        self.__dict__.update(kwargs)
        self.publish()

    def __str__(self):
        if self.severity == Severity.ERROR:
            return "{0}: type={1.type} name={1.name} node={1.node} duration={1.duration} error={1.error}\n{1.full_traceback}".format(
                super(DisruptionEvent, self).__str__(), self)
        return "{0}: type={1.type} name={1.name} node={1.node} duration={1.duration}".format(
            super(DisruptionEvent, self).__str__(), self)


class ClusterHealthValidatorEvent(SctEvent):
    def __init__(self, type, name, status=Severity.CRITICAL, node=None, message=None, error=None, **kwargs):  # pylint: disable=redefined-builtin,too-many-arguments
        super(ClusterHealthValidatorEvent, self).__init__()
        self.name = name
        self.type = type
        self.node = str(node)
        self.severity = status
        self.error = error if error else ''
        self.message = message if message else ''

        self.__dict__.update(kwargs)
        self.publish()

    def __str__(self):
        if self.severity == Severity.NORMAL:
            return "{0}: type={1.type} name={1.name} node={1.node} message={1.message}".format(
                super(ClusterHealthValidatorEvent, self).__str__(), self)
        elif self.severity in (Severity.CRITICAL, Severity.ERROR):
            return "{0}: type={1.type} name={1.name} node={1.node} error={1.error}".format(
                super(ClusterHealthValidatorEvent, self).__str__(), self)
        else:
            return super(ClusterHealthValidatorEvent, self).__str__()


class FullScanEvent(SctEvent):
    def __init__(self, type, ks_cf, db_node_ip, severity=Severity.NORMAL, message=None):   # pylint: disable=redefined-builtin,too-many-arguments
        super(FullScanEvent, self).__init__()
        self.type = type
        self.ks_cf = ks_cf
        self.db_node_ip = db_node_ip
        self.severity = severity
        self.msg = "{0}: type={1.type} select_from={1.ks_cf} on db_node={1.db_node_ip}"
        if message:
            self.message = message
            self.msg += " {1.message}"
        self.publish()

    def __str__(self):
        return self.msg.format(super(FullScanEvent, self).__str__(), self)


class GeminiEvent(SctEvent):
    def __init__(self, type, cmd, result=None):  # pylint: disable=redefined-builtin
        super(GeminiEvent, self).__init__()
        self.type = type
        self.cmd = cmd
        self.msg = "{0}: type={1.type} gemini_cmd={1.cmd}"
        self.result = ""
        if result:
            self.result += "Exit code: {exit_code}\n"
            if result['stdout']:
                self.result += "Command output: {stdout}\n"
                result['stdout'] = result['stdout'].strip().split('\n')[-2:]
            if result['stderr']:
                self.result += "Command error: {stderr}\n"
            self.result = self.result.format(**result)
            if result['exit_code'] != 0 or result['stderr']:
                self.severity = Severity.ERROR
                self.type = 'error'
            self.msg += '\n{1.result}'
        self.publish()

    def __str__(self):
        return self.msg.format(super(GeminiEvent, self).__str__(), self)


class CassandraStressEvent(SctEvent):
    def __init__(self, type, node, severity=Severity.NORMAL, stress_cmd=None, log_file_name=None, errors=None):  # pylint: disable=redefined-builtin,too-many-arguments
        super(CassandraStressEvent, self).__init__()
        self.type = type
        self.node = str(node)
        self.stress_cmd = stress_cmd
        self.log_file_name = log_file_name
        self.severity = severity
        self.errors = errors
        self.publish()

    def __str__(self):
        if self.errors:
            return "{0}: type={1.type} node={1.node}\n{2}".format(
                super(CassandraStressEvent, self).__str__(), self, "\n".join(self.errors))

        return "{0}: type={1.type} node={1.node}\nstress_cmd={1.stress_cmd}".format(
            super(CassandraStressEvent, self).__str__(), self)


class ScyllaBenchEvent(SctEvent):
    def __init__(self, type, node, severity=Severity.NORMAL, stress_cmd=None, log_file_name=None, errors=None):  # pylint: disable=redefined-builtin,too-many-arguments
        super(ScyllaBenchEvent, self).__init__()
        self.type = type
        self.node = str(node)
        self.stress_cmd = stress_cmd
        self.log_file_name = log_file_name
        self.severity = severity
        self.errors = errors
        self.publish()

    def __str__(self):
        if self.errors:
            return "{0}: type={1.type} node={1.node}\n{2}".format(
                super(ScyllaBenchEvent, self).__str__(), self, "\n".join(self.errors))

        return "{0}: type={1.type} node={1.node}\nstress_cmd={1.stress_cmd}".format(
            super(ScyllaBenchEvent, self).__str__(), self)


class YcsbStressEvent(SctEvent):
    def __init__(self, type, node, severity=Severity.NORMAL, stress_cmd=None, log_file_name=None, errors=None):  # pylint: disable=redefined-builtin,too-many-arguments
        super(YcsbStressEvent, self).__init__()
        self.type = type
        self.node = str(node)
        self.stress_cmd = stress_cmd
        self.log_file_name = log_file_name
        self.severity = severity
        self.errors = errors
        self.publish()

    def __str__(self):
        fmt = f"{super(YcsbStressEvent, self).__str__()}: type={self.type} node={self.node}\nstress_cmd={self.stress_cmd}"
        if self.errors:
            errors_str = '\n'.join(self.errors)
            return f"{fmt}\nerrors:\n\n{errors_str}"
        return fmt


class NdbenchStressEvent(YcsbStressEvent):
    pass


class DatabaseLogEvent(SctEvent):  # pylint: disable=too-many-instance-attributes
    def __init__(self, type, regex, severity=Severity.CRITICAL):  # pylint: disable=redefined-builtin
        super(DatabaseLogEvent, self).__init__()
        self.type = type
        self.regex = regex
        self.line_number = 0
        self.line = None
        self.node = None
        self.backtrace = None
        self.raw_backtrace = None
        self.severity = severity

    def add_info(self, node, line, line_number):
        try:
            log_time = dateutil.parser.parse(line.split()[0])
            self.timestamp = log_time.timestamp()
        except ValueError:
            self.timestamp = time.time()
        self.line = line
        self.line_number = line_number
        self.node = str(node)

        # dynamically handle reactor stalls severity
        if self.type == 'REACTOR_STALLED':
            try:
                stall_time = int(re.findall(r'(\d+) ms', line)[0])
                if stall_time <= 2000:
                    self.severity = Severity.NORMAL

            except (ValueError, IndexError):
                LOGGER.warning("failed to read REACTOR_STALLED line=[%s] ", line)

    def add_backtrace_info(self, backtrace=None, raw_backtrace=None):
        if backtrace:
            self.backtrace = backtrace
        if raw_backtrace:
            self.raw_backtrace = raw_backtrace

    def clone_with_info(self, node, line, line_number):
        ret = DatabaseLogEvent(type='', regex='')
        ret.__dict__.update(self.__dict__)
        ret.add_info(node, line, line_number)
        return ret

    def add_info_and_publish(self, node, line, line_number):
        self.add_info(node, line, line_number)
        self.publish()

    def __str__(self):
        if self.backtrace:
            return "{0}: type={1.type} regex={1.regex} line_number={1.line_number} node={1.node}\n{1.line}\n{1.backtrace}".format(
                super(DatabaseLogEvent, self).__str__(), self)

        if self.raw_backtrace:
            return "{0}: type={1.type} regex={1.regex} line_number={1.line_number} node={1.node}\n{1.line}\n{1.raw_backtrace}".format(
                super(DatabaseLogEvent, self).__str__(), self)

        return "{0}: type={1.type} regex={1.regex} line_number={1.line_number} node={1.node}\n{1.line}".format(
            super(DatabaseLogEvent, self).__str__(), self)


class CassandraStressLogEvent(DatabaseLogEvent):
    pass


class GeminiLogEvent(DatabaseLogEvent):
    pass


class SpotTerminationEvent(SctEvent):
    def __init__(self, node, message):
        super(SpotTerminationEvent, self).__init__()
        self.severity = Severity.CRITICAL
        self.node = str(node)
        self.message = message
        self.publish()

    def __str__(self):
        return "{0}: node={1.node} message={1.message}".format(
            super(SpotTerminationEvent, self).__str__(), self)


class PrometheusAlertManagerEvent(SctEvent):  # pylint: disable=too-many-instance-attributes
    _from_str_regexp = re.compile(
        "[^:]+: alert_name=(?P<alert_name>[^ ]+) type=(?P<type>[^ ]+) start=(?P<start>[^ ]+) "
        f"end=(?P<end>[^ ]+) description=(?P<description>[^ ]+) updated=(?P<updated>[^ ]+) state=(?P<state>[^ ]+) "
        f"fingerprint=(?P<fingerprint>[^ ]+) labels=(?P<labels>[^ ]+)")

    def __init__(self,  # pylint: disable=too-many-arguments
                 raw_alert=None, event_str=None, sct_event_str=None, event_type: str = None, severity=Severity.WARNING):
        super().__init__()
        self.severity = severity
        self.type = event_type
        if raw_alert:
            self._load_from_raw_alert(**raw_alert)
        elif event_str:
            self._load_from_event_str(event_str)
        elif sct_event_str:
            self._load_from_sctevent_str(sct_event_str)

    def __str__(self):
        return f"{super().__str__()}: alert_name={self.alert_name} type={self.type} start={self.start} "\
               f"end={self.end} description={self.description} updated={self.updated} state={self.state} "\
               f"fingerprint={self.fingerprint} labels={self.labels}"

    def _load_from_sctevent_str(self, data: str):
        result = self._from_str_regexp.match(data)
        if not result:
            return False
        tmp = result.groupdict()
        if not tmp:
            return False
        tmp['labels'] = json.loads(tmp['labels'])
        for name, value in tmp:
            setattr(self, name, value)
        return True

    def _load_from_event_str(self, data: str):
        try:
            tmp = json.loads(data)
        except Exception:  # pylint: disable=broad-except
            return None
        for name, value in tmp.items():
            if name not in ['annotations', 'description', 'start', 'end', 'updated', 'fingerprint', 'status', 'labels',
                            'state', 'alert_name', 'severity', 'type', 'timestamp', 'severity']:
                return False
            setattr(self, name, value)
        if isinstance(self.severity, str):
            self.severity = getattr(Severity, self.severity)
        return True

    def _load_from_raw_alert(self,  # pylint: disable=too-many-arguments,invalid-name,unused-argument
                             annotations: dict = None, startsAt=None, endsAt=None, updatedAt=None, fingerprint=None,
                             status=None, labels=None, **kwargs):
        self.annotations = annotations
        if self.annotations:
            self.description = self.annotations.get('description', self.annotations.get('summary', ''))
        else:
            self.description = ''
        self.start = startsAt
        self.end = endsAt
        self.updated = updatedAt
        self.fingerprint = fingerprint
        self.status = status
        self.labels = labels
        if self.status:
            self.state = self.status.get('state', '')
        else:
            self.state = ''
        if self.labels:
            self.alert_name = self.labels.get('alertname', '')
        else:
            self.alert_name = ''

    def __eq__(self, other):
        for name in ['alert_name', 'type', 'start', 'end', 'description', 'updated', 'state', 'fingerprint', 'labels']:
            other_value = getattr(other, name, None)
            value = getattr(self, name, None)
            if value != other_value:
                return False
        return True


class TestKiller(Process):
    def __init__(self, timeout_before_kill=2, test_callback=None):
        super(TestKiller, self).__init__()
        self._test_pid = os.getpid()
        self.test_callback = test_callback
        self.timeout_before_kill = timeout_before_kill

    def run(self):
        for event_type, message_data in EVENTS_PROCESSES['MainDevice'].subscribe_events():
            if event_type == 'KillTestEvent':
                time.sleep(self.timeout_before_kill)
                LOGGER.debug("Killing the test")
                if callable(self.test_callback):
                    self.test_callback(message_data)
                    continue
                if not safe_kill(self._test_pid, signal.SIGTERM) or pid_exists(self._test_pid):
                    safe_kill(self._test_pid, signal.SIGKILL)


class EventsFileLogger(Process):  # pylint: disable=too-many-instance-attributes
    def __init__(self, log_dir):
        super(EventsFileLogger, self).__init__()
        self._test_pid = os.getpid()
        self.event_log_base_dir = Path(log_dir, 'events_log')
        self.events_filename = Path(self.event_log_base_dir, 'events.log')
        self.critical_events_filename = Path(self.event_log_base_dir, 'critical.log')
        self.error_events_filename = Path(self.event_log_base_dir, 'error.log')
        self.warning_events_filename = Path(self.event_log_base_dir, 'warning.log')
        self.normal_events_filename = Path(self.event_log_base_dir, 'normal.log')
        self.events_summary_filename = Path(self.event_log_base_dir, 'summary.log')

        for log_file in [self.critical_events_filename, self.error_events_filename,
                         self.warning_events_filename, self.normal_events_filename,
                         self.events_summary_filename]:
            log_file.touch()

        self.level_to_file_mapping = {
            Severity.CRITICAL: self.critical_events_filename,
            Severity.ERROR: self.error_events_filename,
            Severity.WARNING: self.warning_events_filename,
            Severity.NORMAL: self.warning_events_filename,
        }
        self.level_summary = collections.defaultdict(int)

    def run(self):
        LOGGER.info("writing to %s", self.events_filename)

        for _, message_data in EVENTS_PROCESSES['MainDevice'].subscribe_events():
            try:
                msg = "{}: {}".format(message_data.formatted_timestamp, str(message_data).strip())
                with open(self.events_filename, 'a+') as log_file:
                    log_file.write(msg + '\n')

                # update each level log file
                events_filename = self.level_to_file_mapping[message_data.severity]
                with open(events_filename, 'a+') as events_level_file:
                    events_level_file.write(msg + '\n')

                # update the summary file
                self.level_summary[str(Severity(message_data.severity))] += 1
                with open(self.events_summary_filename, 'w') as summary_file:
                    json.dump(dict(self.level_summary), summary_file, indent=4)

                LOGGER.info(msg)
            except Exception:  # pylint: disable=broad-except
                LOGGER.exception("Failed to write event to event.log")


EVENTS_PROCESSES = dict()


def start_events_device(log_dir, timeout=5):  # pylint: disable=redefined-outer-name
    from sdcm.utils.grafana import GrafanaEventAggragator, GrafanaAnnotator

    EVENTS_PROCESSES['MainDevice'] = EventsDevice(log_dir)
    EVENTS_PROCESSES['MainDevice'].start()

    EVENTS_PROCESSES['EVENTS_FILE_LOOGER'] = EventsFileLogger(log_dir)
    EVENTS_PROCESSES['EVENTS_GRAFANA_ANNOTATOR'] = GrafanaAnnotator()
    EVENTS_PROCESSES['EVENTS_GRAFANA_AGGRAGATOR'] = GrafanaEventAggragator()

    EVENTS_PROCESSES['EVENTS_FILE_LOOGER'].start()
    EVENTS_PROCESSES['EVENTS_GRAFANA_ANNOTATOR'].start()
    EVENTS_PROCESSES['EVENTS_GRAFANA_AGGRAGATOR'].start()

    try:
        EVENTS_PROCESSES['MainDevice'].wait_till_event_loop_is_working(number_of_events=20)
    except RuntimeError:
        LOGGER.error("EVENTS_PROCESSES['MainDevice'] event loop failed to deliver 20 test events with no loss")
        raise

    # default filters
    EVENTS_PROCESSES['default_filter'] = []
    EVENTS_PROCESSES['default_filter'] += [DbEventsFilter(type='BACKTRACE', line='Rate-limit: supressed')]
    EVENTS_PROCESSES['default_filter'] += [DbEventsFilter(type='BACKTRACE', line='Rate-limit: suppressed')]


def stop_events_device():
    LOGGER.info("Stopping Events consumers...")
    processes = ['EVENTS_FILE_LOOGER', 'EVENTS_GRAFANA_ANNOTATOR', 'EVENTS_GRAFANA_AGGRAGATOR', 'MainDevice']
    LOGGER.debug("Signalling events consumers to terminate...")
    for proc_name in processes:
        if proc_name in EVENTS_PROCESSES:
            EVENTS_PROCESSES[proc_name].terminate()
    LOGGER.debug("Waiting for Events consumers to finish...")
    for proc_name in processes:
        if proc_name in EVENTS_PROCESSES:
            EVENTS_PROCESSES[proc_name].join(timeout=60)
    LOGGER.info("All Events consumers stopped.")


def set_grafana_url(url):
    EVENTS_PROCESSES['EVENTS_GRAFANA_AGGRAGATOR'].set_grafana_url(url)


def get_logger_event_summary():
    with open(EVENTS_PROCESSES['EVENTS_FILE_LOOGER'].events_summary_filename) as summary_file:
        output = json.load(summary_file)
    return output


atexit.register(stop_events_device)
