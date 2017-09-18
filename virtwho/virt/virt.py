"""
Module for abstraction of all virtualization backends, part of virt-who

Copyright (C) 2014 Radek Novacek <rnovacek@redhat.com>

This program is free software; you can redistribute it and/or
modify it under the terms of the GNU General Public License
as published by the Free Software Foundation; either version 2
of the License, or (at your option) any later version.

This program is distributed in the hope that it will be useful,
but WITHOUT ANY WARRANTY; without even the implied warranty of
MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
GNU General Public License for more details.

You should have received a copy of the GNU General Public License
along with this program; if not, write to the Free Software
Foundation, Inc., 51 Franklin Street, Fifth Floor, Boston, MA  02110-1301, USA.
"""

import sys
import time
import logging
from operator import itemgetter
from datetime import datetime
from threading import Thread, Event
import json
import hashlib
import re
import fnmatch
import socket
from virtwho.config import NotSetSentinel, Satellite5DestinationInfo, \
    Satellite6DestinationInfo, DefaultDestinationInfo
from virtwho.manager import ManagerError, ManagerThrottleError, ManagerFatalError
from virtwho import MinimumSendInterval
from virtwho.datastore import Datastore

try:
    from collections import OrderedDict
except ImportError:
    # Python 2.6 doesn't have OrderedDict, we need to have our own
    from virtwho.util import OrderedDict

from virtwho import DefaultInterval


class VirtError(Exception):
    pass


class Guest(object):
    """
    This class represents one virtualization guest running on some
    host/hypervisor.
    """

    STATE_UNKNOWN = 0      # unknown state
    STATE_RUNNING = 1      # running
    STATE_BLOCKED = 2      # blocked on resource
    STATE_PAUSED = 3       # paused by user
    STATE_SHUTINGDOWN = 4  # guest is being shut down
    STATE_SHUTOFF = 5      # shut off
    STATE_CRASHED = 6      # crashed
    STATE_PMSUSPENDED = 7  # suspended by guest power management

    def __init__(self,
                 uuid,
                 virt,
                 state,
                 hypervisorType=None):
        """
        Create new guest instance that will be sent to the subscription manager.

        `uuid` is unique identification of the guest.

        `virt` is a `Virt` class instance that represents virtualization hypervisor
        that owns the guest.

        `state` is a number that represents the state of the guest (stopped, running, ...)
        """
        self.uuid = uuid
        self.virtWhoType = virt.CONFIG_TYPE
        self.state = state

    def __repr__(self):
        return 'Guest({0.uuid!r}, {0.virtWhoType!r}, {0.state!r})'.format(self)

    def toDict(self):
        d = OrderedDict((
            ('guestId', self.uuid),
            ('state', self.state),
            ('attributes', {
                'virtWhoType': self.virtWhoType,
                'active': 1 if self.state in (self.STATE_RUNNING, self.STATE_PAUSED) else 0
            }),
        ))
        return d


class Hypervisor(object):
    """
    A model for information about a hypervisor
    """

    CPU_SOCKET_FACT = 'cpu.cpu_socket(s)'
    HYPERVISOR_TYPE_FACT = 'hypervisor.type'
    HYPERVISOR_VERSION_FACT = 'hypervisor.version'

    def __init__(self, hypervisorId, guestIds=None, name=None, facts=None):
        """
        Create a new Hypervisor that will be sent to subscription manager

        'hypervisorId': the unique identifier for this hypervisor

        'guestIds': a list of Guests

        'name': the hostname, if available
        """
        self.hypervisorId = hypervisorId
        self.guestIds = guestIds or []
        self.name = name
        self.facts = facts

    def __repr__(self):
        return 'Hypervisor({0.hypervisorId!r}, {0.guestIds!r}, {0.name!r}, {0.facts!r})'.format(self)

    def toDict(self):
        d = OrderedDict((
            ('hypervisorId', {'hypervisorId': self.hypervisorId}),
            ('name', self.name),
            ('guestIds', sorted([g.toDict() for g in self.guestIds], key=itemgetter('guestId')))
        ))
        if self.name is None:
            del d['name']
        if self.facts is not None:
            d['facts'] = self.facts
        return d

    def __str__(self):
        return str(self.toDict())

    def getHash(self):
        sortedRepresentation = json.dumps(self.toDict(), sort_keys=True)
        return hashlib.sha256(sortedRepresentation).hexdigest()


class AbstractVirtReport(object):
    '''
    An abstract report from virt backend.
    '''
    # The report was just collected, but is not yet being reported
    STATE_CREATED = 1
    # The report is being processed by server
    STATE_PROCESSING = 2
    # The report has been processed by server
    STATE_FINISHED = 3
    # Failed to process the report by server
    STATE_FAILED = 4
    # Processing the report on server was canceled
    STATE_CANCELED = 5

    def __init__(self, config, state=STATE_CREATED):
        self._config = config
        self._state = state

    def __repr__(self):
        return '{1}({0.config!r}, {0.state!r})'.format(self, self.__class__.__name__)

    @property
    def config(self):
        return self._config

    @property
    def state(self):
        return self._state

    @state.setter
    def state(self, value):
        self._state = value

    @property
    def hash(self):
        return hash(self)


class ErrorReport(AbstractVirtReport):
    '''
    Report that virt backend fails. Used in oneshot mode to inform
    main thread that no data are coming.
    '''


class DomainListReport(AbstractVirtReport):
    '''
    Report from virt backend about list of virtual guests on given system.
    '''
    def __init__(self, config, guests, hypervisor_id=None, state=AbstractVirtReport.STATE_CREATED):
        super(DomainListReport, self).__init__(config, state)
        self._guests = guests
        self._hypervisor_id = hypervisor_id

    def __repr__(self):
        return 'DomainListReport({0.config!r}, {0.guests!r}, {0.hypervisor_id!r}, {0.state!r})'.format(self)

    @property
    def guests(self):
        return self._guests

    @property
    def hypervisor_id(self):
        return self._hypervisor_id

    @property
    def hash(self):
        return hashlib.sha256(
            json.dumps(
                sorted([g.toDict() for g in self.guests], key=itemgetter('guestId')),
                sort_keys=True) +
            str(self.hypervisor_id)
        ).hexdigest()


class HostGuestAssociationReport(AbstractVirtReport):
    '''
    Report from virt backend about host/guest association on given hypervisor.
    '''
    def __init__(self, config, assoc, state=AbstractVirtReport.STATE_CREATED,
                 exclude_hosts=None, filter_hosts=None):
        super(HostGuestAssociationReport, self).__init__(config, state)
        self._assoc = assoc
        if exclude_hosts is None:
            try:
                exclude_hosts = self._config.exclude_hosts
            except AttributeError:
                # We do not have a config that has this attribute
                pass
        if filter_hosts is None:
            try:
                filter_hosts = self._config.filter_hosts
            except AttributeError:
                # We do not have a config with this attribute
                pass
        self.exclude_hosts = exclude_hosts
        self.filter_hosts = filter_hosts

    def __repr__(self):
        return 'HostGuestAssociationReport({0.config!r}, {0._assoc!r}, {0.state!r})'.format(self)

    def _filter(self, host, filterlist):
        for i in filterlist:
            if fnmatch.fnmatch(host.lower(), i.lower()):
                # match is found
                return True
            try:
                if re.match("^" + i + "$", host, re.IGNORECASE):
                    # match is found
                    return True
            except:
                pass
        # no match
        return False

    @property
    def association(self):
        # Apply filter
        logger = logging.getLogger("virtwho")
        assoc = []
        for host in self._assoc['hypervisors']:
            if self.exclude_hosts is not None and self._filter(
                    host.hypervisorId, self.exclude_hosts):
                logger.debug("Skipping host '%s' because its uuid is excluded", host.hypervisorId)
                continue

            if self.filter_hosts is not None and not self._filter(
                    host.hypervisorId,self.filter_hosts):
                logger.debug("Skipping host '%s' because its uuid is not included", host.hypervisorId)
                continue

            assoc.append(host)
        return {'hypervisors': assoc}

    @property
    def serializedAssociation(self):
        return {
            'hypervisors': sorted([h.toDict() for h in self.association['hypervisors']], key=itemgetter('hypervisorId'))
        }

    @property
    def hash(self):
        return hashlib.sha256(json.dumps(self.serializedAssociation, sort_keys=True)).hexdigest()


class IntervalThread(Thread):
    def __init__(self, logger, config, shared_data, source=None, dest=None,
                 terminate_event=None, interval=None, oneshot=False):
        self.logger = logger
        self.config = config
        # Datastore for sharing data between threads
        self.shared_data = shared_data
        self.source = source
        self.dest = dest
        self._internal_terminate_event = Event()
        self.terminate_event = terminate_event or self._internal_terminate_event
        self.interval = interval or config.interval or DefaultInterval
        self._oneshot = oneshot
        super(IntervalThread, self).__init__()

    def wait(self, wait_time):
        """
        Wait `wait_time` seconds, could be interrupted by setting _terminate_event or _internal_terminate_event.
        """
        for i in range(wait_time):
            if self.is_terminated():
                break
            time.sleep(1)

    def is_terminated(self):
        """

        @return: Returns true if either the internal terminate event is set or
                 the terminate event given in the init is set
        """
        return self._internal_terminate_event.is_set() or \
            self.terminate_event.is_set()

    def stop(self):
        """
        Causes this thread to stop at the next idle moment
        """
        self._internal_terminate_event.set()

    def _gather_and_report(self):
        """
        This method gather data and report them. This method could be
        reimplemented in in subclass (e.g. do not gather data, when
        no consumer is reachable).
        :return: None
        """
        data_to_send = self._get_data()
        self._send_data(data_to_send)

    def _run(self):
        """
        This method could be reimplemented in subclass to provide
        it's own way of waiting for changes (like event monitoring)
        """
        self.prepare()
        while not self.is_terminated():
            start_time = datetime.now()

            self._gather_and_report()

            if self._oneshot:
                self._internal_terminate_event.set()
                break
            end_time = datetime.now()

            delta = end_time - start_time
            # for python2.6, 2.7 has total_seconds method
            delta_seconds = ((
                             delta.days * 86400 + delta.seconds) * 10 ** 6 +
                             delta.microseconds) / 10 ** 6

            wait_time = self.interval - int(delta_seconds)

            if wait_time < 0:
                self.logger.debug(
                    "Getting the data took longer than the configured "
                    "interval. Trying again immediately.")
                continue

            self.wait(wait_time)

    def _get_data(self):
        """
        This method gathers data from the source provided to the thread
        @return: The data from the source
        """
        raise NotImplementedError("Should be implemented in subclasses")

    def _send_data(self, data_to_send):
        """
        @param data_to_send: The data to be given to the dest
        """
        raise NotImplementedError("Should be implemented in subclasses")

    def run(self):
        """
        Wrapper around `_run` method that just catches the error messages.
        """
        self.logger.debug("Thread '%s' started", self.config.name)
        try:
            while not self.is_terminated():
                has_error = False
                try:
                    self._run()
                except VirtError as e:
                    if not self.is_terminated():
                        self.logger.error("Thread '%s' fails with error: %s",
                                          self.config.name, str(e))
                        has_error = True
                except Exception:
                    if not self.is_terminated():
                        self.logger.exception("Thread '%s' fails with "
                                              "exception:", self.config.name)
                        has_error = True

                if self.is_terminated():
                    self.logger.debug("Thread '%s' terminated",
                                      self.config.name)
                    self._internal_terminate_event.set()
                    self.cleanup()
                    return

                if has_error:
                    self._send_data(ErrorReport(self.config))

                if self._oneshot:
                    self.logger.debug("Thread '%s' stopped after running once",
                                      self.config.name)
                    self._internal_terminate_event.set()
                    return

                self.logger.info("Waiting %s seconds before performing action"
                                 " again '%s'", self.interval, self.config.name)
                self.wait(self.interval)
        except KeyboardInterrupt:
            self.logger.debug("Thread '%s' interrupted", self.config.name)
            self.cleanup()
            sys.exit(1)

    def cleanup(self):
        """
        Perform cleaning up actions before termination.
        """
        pass

    def prepare(self):
        """
        Do pre-mainloop initialization of the source and dest,
        for example logging in.
        """
        pass

    @staticmethod
    def handle_429(retry_after, number_of_failures):
        """
        @param retry_after: The value of the Retry-After header (if included)
        @type retry_after: int
        @param number_of_failures: The number of failures that have happened attempting the same
        request.
        @type retry_after: int

        @return: The number of seconds that should be waited before retrying
        @rtype: int
        """
        wait_time = retry_after
        if wait_time and wait_time >= MinimumSendInterval:
            return wait_time
        if number_of_failures > 0:
            wait_time = MinimumSendInterval * number_of_failures
        else:
            wait_time = MinimumSendInterval
        return wait_time


class DestinationThread(IntervalThread):
    """
    This class is a thread that pulls reports from the datastore and sends them
    to the actual destination (candlepin, Satellite, etc) using a manager
    object.

    This class should work so long as the destination is a Manager object.
    """

    def __init__(self, logger, config, shared_data, source_keys=None, options=None,
                 source=None, dest=None, terminate_event=None, interval=None,
                 oneshot=False):
        """
        @param source_keys: A list of keys to be used to retrieve info from
        the source
        @type source_keys: list

        @param source: The source to pull from
        @type source: Datastore

        @param dest: The destination object to use to actually send the data
        @type dest: Manager

        @param shared_data: The object for sharing data between threads
        @type shared_data: Datastore
        """
        if not isinstance(source_keys, list):
            raise ValueError("Source keys must be a list")
        self.is_initial_run = True
        self.source_keys = source_keys
        self.uuid = None
        self.last_report_for_source = {}  # Source_key to hash of last report
        self.options = options
        self.reports_to_print = []  # A list of reports we would send but are
        #  going to print instead, to be used by the owner of the thread
        # after the thread has been killed
        super(DestinationThread, self).__init__(logger, config,
                                                shared_data=shared_data,
                                                source=source,
                                                dest=dest,
                                                terminate_event=terminate_event,
                                                interval=interval,
                                                oneshot=oneshot)
        # The polling interval has not been implemented as configurable yet
        # Until the config includes the polling_interval attribute
        # this will end up being the interval.
        try:
            polling_interval = self.config.polling_interval
        except AttributeError:
            polling_interval = self.interval
        self.polling_interval = polling_interval or self.interval
        # This is used when there is some reason to modify how long we wait
        # EX when we get a 429 back from the server, this value will be the
        # value of the retry_after header.
        self.interval_modifier = 0

    def _get_data(self):
        """
        Gets the latest report from the source for each source_key
        @return: dict
        """
        if self.is_initial_run:
            return self._get_data_initial()
        return self._get_data_common(self.source_keys)

    def _get_data_common(self, source_keys, ignore_duplicates=False, log_missing_reports=True):
        reports = {}
        for source_key in source_keys:
            report = self.source.get(source_key, NotSetSentinel)

            if report is None or report is NotSetSentinel:
                if log_missing_reports:
                    self.logger.debug("No report available for source: %s" %
                                      source_key)
                continue
            if not ignore_duplicates and report.hash == self.last_report_for_source.get(source_key,
                                                                                    None):
                self.logger.debug('Duplicate report found for config "%s", ignoring',
                                  report.config.name)
                continue
            reports[source_key] = report
        return reports

    def _get_data_initial(self):
        """
        Waits for each source in self.source_keys to have a value returned from the datastore. This
        does not check to see if the data has been previously sent. This method will wait for a
        maximum of the self.interval period of time, then return whatever it has gathered thus far.
        If data for each source key is gathered before the interval has expired, this method will
        return.
        @return: dict
        """
        reports = {}
        while not reports and not self.is_terminated():
            source_keys_remaining = set(self.source_keys)
            time_waited = 0
            while len(source_keys_remaining) > 0 and time_waited < self.interval and not self.is_terminated():
                found_reports = self._get_data_common(source_keys_remaining,
                                                      ignore_duplicates=True,
                                                      log_missing_reports=False)
                reports.update(found_reports)
                source_keys_remaining.difference_update(found_reports.keys())
                if len(source_keys_remaining) > 0:
                    sleep_time = 1
                    time.sleep(sleep_time)
                    time_waited += sleep_time
        self.is_initial_run = False
        return reports

    def _update_consumers(self, data_to_send):
        """Try to update shared dictionary of consumers parsing data_to_send"""

        def update_consumers(key, consumers, _data_to_send):
            """
            This method is used for updating shared dictionary of
            host-to-guest mapping consumers
            :param key: Unused argument
            :param consumers: Current dictionary of consumers (keys are hypervisor_uuid and
                values are lists of destination consumers, respectively hostnames of canlepin servers)
            :param _data_to_send: Data to be reported to consumer
            :return: New dictionary of consumers
            """
            for value in _data_to_send.values():
                if hasattr(value, 'association'):
                    for hypervisor in value.association['hypervisors']:
                        if hypervisor.hypervisorId not in consumers.keys():
                            consumers[hypervisor.hypervisorId] = [value.config.rhsm_hostname]
                        elif value.config.rhsm_hostname not in consumers[hypervisor.hypervisorId]:
                            consumers[hypervisor.hypervisorId].append(value.config.rhsm_hostname)
            return consumers

        self.shared_data.update('consumers', {}, update_consumers, data_to_send)

    def _remove_unreachable_consumer(self, unreachable_consumer):
        """Try to remove unreachable consumer from shared dictionary of consumers"""

        def remove_consumer(key, consumers, _unreachable_consumer):
            for consumer_list in consumers.values():
                if _unreachable_consumer in consumer_list:
                    consumer_list.remove(_unreachable_consumer)
            return consumers

        self.shared_data.update('consumers', {}, remove_consumer, unreachable_consumer)

    def _send_data(self, data_to_send):
        """
        Processes the data_to_send and sends it using the dest object.
        @param data_to_send: A dict of source_keys, report
        @type: dict
        """

        if not data_to_send:
            self.logger.debug('No data to send, waiting for next interval')
            return
        if isinstance(data_to_send, ErrorReport):
            self.logger.info('Error report received')
            if data_to_send.config.name != self.config.name:
                self.stop()
            return

        all_hypervisors = []  # All the Host-guest mappings together
        domain_list_reports = []  # Source_keys of DomainListReports
        reports_batched = []  # Source_keys of reports to be sent as one
        sources_sent = []  # Sources we have dealt with this run
        sources_erred = []  # Sources with some problems

        total_hypervisors = 0
        total_guests = 0

        self._update_consumers(data_to_send)

        # Reports of different types are handled differently
        for source_key, report in data_to_send.iteritems():
            if getattr(self.config, 'owner', None) is None:
                # If the owner on our config is not defined, set it to the first report that
                # we've found. This should be ok because destination threads should not be run for
                # more than one owner.
                self.config.owner = report.config.owner
            if isinstance(report, DomainListReport):
                # These are sent one at a time to the destination
                domain_list_reports.append(source_key)
                continue
            if isinstance(report, HostGuestAssociationReport):
                # These reports are put into one report to send at once
                all_hypervisors.extend(report.association['hypervisors'])
                # Keep track of those reports that we have
                reports_batched.append(source_key)
                # Print information about host-to-quest mapping for this report
                mapping = report.association
                hypervisor_count = len(mapping['hypervisors'])
                guest_count = sum(len(hypervisor.guestIds) for hypervisor in mapping['hypervisors'])
                self.logger.info('Hosts-to-guests mapping for config "%s": %d hypervisors and %d guests found',
                                 report.config.name, hypervisor_count, guest_count)
                total_hypervisors += hypervisor_count
                total_guests += guest_count
                continue
            if isinstance(report, ErrorReport):
                # These indicate an error that came from this source
                # Log it and move along.
                # if it was recoverable we'll get something else next time.
                # if it was not recoverable we'll see this again from this
                # source. Thus we'll just log this at the debug level.
                self.logger.debug('ErrorReport received for source: %s' % source_key)
                if self._oneshot:
                    # Consider this source dealt with if we are in oneshot mode
                    sources_erred.append(source_key)

        # Modify the batched dict to be in the form expected for
        # HostGuestAssociationReports
        all_hypervisors_dict = {'hypervisors': all_hypervisors}
        batch_host_guest_report = HostGuestAssociationReport(self.config, all_hypervisors_dict)

        if all_hypervisors:
            result = None
            # Try to actually do the checkin whilst being mindful of the
            # rate limit (retrying where necessary)
            num_429_received = 0
            while result is None and not self.is_terminated():
                try:
                    self.logger.info('Sending updated Host-to-guest mapping to "{owner}" including '
                                     '{num_hypervisors} hypervisors and {num_guests} '
                                     'guests'.format(owner=self.config.owner,
                                                     num_hypervisors=total_hypervisors,
                                                     num_guests=total_guests))
                    result = self.dest.hypervisorCheckIn(
                            batch_host_guest_report,
                            options=self.options)
                    break
                except ManagerThrottleError as e:
                    if self._oneshot:
                        self.logger.debug('429 encountered while performing hypervisor checkin in '
                                          'oneshot mode, not retrying')
                        sources_erred.extend(reports_batched)
                        break
                    num_429_received += 1
                    retry_after = self.handle_429(e.retry_after, num_429_received)
                    self.logger.debug("429 encountered while performing "
                                      "hypervisor check in.\n"
                                      "Trying again in "
                                      "%s", retry_after)
                    self.interval_modifier = retry_after
                except (ManagerError, ManagerFatalError) as err:
                    self.logger.exception("Error during hypervisor "
                                          "checkin: %s" % err)
                    if self._oneshot:
                        sources_erred.extend(reports_batched)
                    break
                except socket.error as err:
                    # FIXME: we need some straightforward way of getting hostname of candlepin server
                    # for given destination thread
                    try:
                        candlepin_server_hostname = batch_host_guest_report.config['rhsm_hostname']
                    except KeyError:
                        candlepin_server_hostname = self.dest.rhsm_config.get('server', 'hostname')
                    self.logger.error('Error: %s during connection to: %s' % (err, candlepin_server_hostname))
                    self._remove_unreachable_consumer(candlepin_server_hostname)
                    break
                self.wait(wait_time=self.interval_modifier)
                self.interval_modifier = 0

            initial_job_check = True
            num_429_received = 0
            # Poll for async results if async (retrying where necessary)
            while result and batch_host_guest_report.state not in [
                AbstractVirtReport.STATE_CANCELED,
                AbstractVirtReport.STATE_FAILED,
                AbstractVirtReport.STATE_FINISHED] and not self.is_terminated():
                if self.interval_modifier != 0:
                    wait_time = self.interval_modifier
                    self.interval_modifier = 0
                else:
                    wait_time = self.polling_interval
                if not initial_job_check:
                    self.wait(wait_time=wait_time)
                try:
                    self.dest.check_report_state(batch_host_guest_report)
                except ManagerThrottleError as e:
                    if self._oneshot:
                        self.logger.debug('429 encountered when checking job state in '
                                          'oneshot mode, not retrying')
                        sources_sent.extend(reports_batched)
                        break
                    retry_after = self.handle_429(e.retry_after, num_429_received)
                    self.logger.debug('429 encountered while checking job '
                                      'state, checking again in "%s"', retry_after)
                    self.interval_modifier = retry_after
                except (ManagerError, ManagerFatalError):
                    self.logger.exception("Error during job check: ")
                    if self._oneshot:
                        sources_sent.extend(reports_batched)
                    break
                initial_job_check = False

            # If the batch report did not reach the finished state
            # we do not want to update which report we last sent (as we
            # might want to try to send the same report again next time)
            if batch_host_guest_report.state == \
                    AbstractVirtReport.STATE_FINISHED:
                # Update the hash of the info last sent for each source
                # included in the successful report
                for source_key in reports_batched:
                    self.last_report_for_source[source_key] = data_to_send[
                        source_key].hash
                    sources_sent.append(source_key)

        # Send each Domain Guest List Report if necessary
        for source_key in domain_list_reports:
            report = data_to_send[source_key]
            if not self.options.print_:
                retry = True
                num_429_received = 0
                while retry and not self.is_terminated():  # Retry if we encounter a 429
                    try:
                        self.dest.sendVirtGuests(report, options=self.options)
                        sources_sent.append(source_key)
                        self.last_report_for_source[source_key] = data_to_send[
                            source_key].hash
                        retry = False
                    except ManagerThrottleError as e:
                        if self._oneshot:
                            self.logger.debug('429 encountered when sending virt guests in '
                                              'oneshot mode, not retrying')
                            sources_erred.append(source_key)
                            break
                        num_429_received += 1
                        retry_after = self.handle_429(e.retry_after, num_429_received)
                        self.logger.debug('429 encountered when sending virt '
                                          'guests.'
                                          'Retrying after: %s', retry_after)
                        self.wait(wait_time=retry_after)
                    except (ManagerError, ManagerFatalError):
                        self.logger.exception("Fatal error during send virt "
                                              "guests: ")
                        if self._oneshot:
                            sources_erred.append(source_key)
                        retry = False  # Only retry on 429

        consumers = self.shared_data.get('consumers', {})
        self.logger.debug('Current dictionary of consumers: %s' % consumers)

        # Were all sources handled at lease by one report?
        all_sources_handled = all((source_key in sources_sent or source_key in sources_erred)
                                  for source_key in self.source_keys)

        # Terminate this thread if we have sent one report for each source
        if all_sources_handled and self._oneshot:
                if not self.options.print_:
                    self.logger.debug('At least one report for each connected source has been sent. Terminating.')
                else:
                    self.logger.debug('All info to print has been gathered. Terminating.')
                self.stop()

        if self._oneshot:
            # Remove sources we have sent (or dealt with) so that we don't
            # do extra work on the next run, should we have missed any sources
            self.source_keys = [source_key for source_key in self.source_keys
                                if source_key not in sources_sent]
        return


class Satellite5DestinationThread(DestinationThread):

    def _send_data(self, data_to_send):
        """
        Processes the data_to_send and sends it using the dest object.
        @param data_to_send: A dict of source_keys, report
        @type: dict
        """
        if not data_to_send:
            self.logger.debug('No data to send, waiting for next interval')
            return
        if isinstance(data_to_send, ErrorReport):
            self.logger.info('Error report received, shutting down')
            self.stop()
            return
        sources_sent = []  # Sources we have dealt with this run
        sources_erred = []  # Sources that have had some error this run

        # Reports of different types are handled differently
        for source_key, report in data_to_send.iteritems():
            if isinstance(report, DomainListReport):
                self.logger.warning("virt-who does not support sending local"
                                    "hypervisor data to satellite; use "
                                    "rhn-virtualization-host "
                                    "instead; Dropping offending source: '%s'",
                                    source_key)
                # Do not attempt to send such reports, satellite 5 does not
                # know what to do with such reports. Since we do not know
                # what to do, and virt backends will not change their output
                # without a restart, drop this source from those that we check.
                sources_erred.append(source_key)
                continue
            if isinstance(report, HostGuestAssociationReport):
                # We cannot (effectively) batch reports to be checked in in
                # one communication via Satellite 5. As such we'll just do a
                # hypervisor check in for each report of that type.
                result = None
                num_429_received = 0
                while result is None and not self.is_terminated():
                    try:
                        result = self.dest.hypervisorCheckIn(
                                report,
                                options=self.options)
                        self.last_report_for_source[source_key] = report.hash
                        sources_sent.append(source_key)
                        break
                    except ManagerThrottleError as e:
                        if self._oneshot:
                            self.logger.debug('429 encountered during hypervisor checkin in '
                                             'oneshot mode, not retrying')
                            sources_erred.append(source_key)
                            break
                        num_429_received += 1
                        retry_after = self.handle_429(e.retry_after, num_429_received)
                        self.logger.debug("429 encountered while performing "
                                          "hypervisor check in.\n"
                                          "Trying again in "
                                          "%s", retry_after)
                        self.interval_modifier = retry_after
                    except ManagerFatalError:
                        self.logger.exception("Fatal error during hypervisor "
                                              "checkin: ")
                        sources_erred.append(source_key)
                        break
            if isinstance(report, ErrorReport):
                # These indicate an error that came from this source
                # Log it and move along.
                # if it was recoverable we'll get something else next time.
                # if it was not recoverable we'll see this again from this
                # source. Thus we'll just log this at the debug level.
                self.logger.debug('ErrorReport received for source: %s' % source_key)
                if self._oneshot:
                    # Consider this source dealt with if we are in oneshot mode
                    sources_sent.append(source_key)

        # Terminate this thread if we have sent one report for each source
        if all(source_key in sources_sent for source_key in self.source_keys)\
                and self._oneshot:
            if not self.options.print_:
                self.logger.debug('At least one report for each connected '
                                  'source has been sent. Terminating.')
            else:
                self.logger.debug('All info to print has been gathered. '
                                  'Terminating.')
            self.stop()
        if self._oneshot:
            # Remove sources we have sent (or dealt with) so that we don't
            # do extra work on the next run, should we have missed any sources
            self.source_keys = [source_key for source_key in self.source_keys
                                if source_key not in sources_sent]
        return


class Virt(IntervalThread):
    """
    Virtualization backend abstract class.

    This class must be inherited for each of the virtualization backends.

    Run `start` method to start obtaining data about virtual guests. The data
    will be pushed to the dest(ination) that is parameter of the `__init__`
    method.
    """

    def __init__(self, logger, config, shared_data, dest, terminate_event=None,
                 interval=None, oneshot=False):
        super(Virt, self).__init__(logger, config, shared_data, dest=dest,
                                   terminate_event=terminate_event,
                                   interval=interval, oneshot=oneshot)

    @classmethod
    def __subclasses_list(cls):
        # Imports can't be top-level, it would be circular dependency
        import virtwho.virt.libvirtd  # flake8: noqa
        import virtwho.virt.esx  # flake8: noqa
        import virtwho.virt.xen  # flake8: noqa
        import virtwho.virt.rhevm  # flake8: noqa
        import virtwho.virt.vdsm  # flake8: noqa
        import virtwho.virt.hyperv  # flake8: noqa
        import virtwho.virt.fakevirt  # flake8: noqa

        return [subcls for subcls in cls.__subclasses__()]

    @classmethod
    def from_config(cls, logger, config, shared_data, dest,
                    terminate_event=None, interval=None, oneshot=False):
        """
        Create instance of inherited class based on the config.
        """

        for subcls in cls.__subclasses_list():
            if config.type == subcls.CONFIG_TYPE:
                return subcls(logger, config, shared_data, dest,
                              terminate_event=terminate_event,
                              interval=interval, oneshot=oneshot)
        raise KeyError("Invalid config type: %s" % config.type)

    @classmethod
    def hypervisor_types(cls):
        """
        Return list of textual representations of supported hypervisors
        """

        return [subcls.CONFIG_TYPE for subcls in cls.__subclasses_list() if subcls.CONFIG_TYPE != 'fake']

    def start_sync(self):
        """
        This method is same as `start()` but runs synchronously, it does NOT
        create new thread.

        Use it only in specific cases!
        """
        self._run()

    def _get_report(self):
        if self.isHypervisor():
            return HostGuestAssociationReport(self.config, self.getHostGuestMapping())
        else:
            return DomainListReport(self.config, self.listDomains())

    # TODO: Reimplement each virt subclass as a source
    def _get_data(self):
        """
        Gathers the data from the source.
        Could be overridden to specify how to get data other than a report.
        For example in destination threads.
        @return: The data from the source to be passed along to the dest
        """
        return self._get_report()

    def _send_data(self, data_to_send):
        if self.is_terminated():
            sys.exit(0)
        self.logger.info('Report for config "%s" gathered, placing in '
                          'datastore', data_to_send.config.name)
        self.dest.put(self.config.name, data_to_send)

    def isHypervisor(self):
        """
        Return True if the virt instance represents hypervisor environment
        otherwise it represents just one virtual server.
        """
        return True

    def getHostGuestMapping(self):
        """
        If subclass doesn't reimplement the `_run` method, it should
        reimplement either this method or `listDomains` method, based on
        return value of isHypervisor method.
        """
        raise NotImplementedError('This should be reimplemented in subclass')

    def listDomains(self):
        """
        If subclass doesn't reimplement the `_run` method, it should
        reimplement either this method or `getHostGuestMapping` method, based on
        return value of isHypervisor method.
        """
        raise NotImplementedError('This should be reimplemented in subclass')

    def are_consumers_reachable(self, hypervisor_uuid):
        """
        This function tries to guess if there is at least one consumers for given hypervisor_uuid.
        :return: Return true, when virt-who was able to send data at least to one candlepin server.
        """

        try:
            consumers = self.shared_data.get(key='consumers', default=None)
        except KeyError:
            # There were no attempt to report host-to-guest mapping in the past. We will be positive
            # and we will estimate there is at least one reachable consumer.
            consumers_reachable = True
        else:
            try:
                if len(consumers[hypervisor_uuid]) > 0:
                    consumers_reachable = True
                else:
                    consumers_reachable = False
            except KeyError:
                # Similar example as before. It wasn't reported anything about given hypervisor, but
                # only for other hypervisors
                consumers_reachable = True
        return consumers_reachable


info_to_destination_class = {
    Satellite5DestinationInfo: Satellite5DestinationThread,
    Satellite6DestinationInfo: DestinationThread,
    DefaultDestinationInfo: DestinationThread,
}
