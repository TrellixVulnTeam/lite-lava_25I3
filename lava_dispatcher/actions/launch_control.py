#!/usr/bin/python

# Copyright (C) 2011 Linaro Limited
#
# Author: Paul Larson <paul.larson@linaro.org>
#
# This file is part of LAVA Dispatcher.
#
# LAVA Dispatcher is free software; you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation; either version 2 of the License, or
# (at your option) any later version.
#
# LAVA Dispatcher is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with this program; if not, see <http://www.gnu.org/licenses>.

import os
import logging
import tempfile
import urlparse
import xmlrpclib

from lava_tool.authtoken import AuthenticatingServerProxy, MemoryAuthBackend

from linaro_dashboard_bundle.io import DocumentIO
from linaro_dashboard_bundle.evolution import DocumentEvolution

from lava_dispatcher.actions import BaseAction
from lava_dispatcher.errors import OperationFailed
from lava_dispatcher.test_data import create_attachment
import lava_dispatcher.utils as utils


class GatherResultsError(Exception):
    def __init__(self, msg, bundles=[]):
        super(GatherResultsError, self).__init__(msg)
        self.bundles = bundles


def _get_dashboard(server, token):
    if not server.endswith("/"):
        server = ''.join([server, "/"])

    #add backward compatible for 'dashboard/'-end URL
    #Fix it: it's going to be deleted after transition
    if server.endswith("dashboard/"):
        server = ''.join([server, "xml-rpc/"])
        logging.warn("Please use whole endpoint URL not just end with 'dashboard/', 'xml-rpc/' is added automatically now!!!")

    parsed_server = urlparse.urlparse(server)
    auth_backend = MemoryAuthBackend([])
    if parsed_server.username:
        if token:
            userless_server = '%s://%s' % (
                parsed_server.scheme, parsed_server.hostname)
            if parsed_server.port:
                userless_server += ':' + str(parsed_server.port)
            userless_server += parsed_server.path
            auth_backend = MemoryAuthBackend(
                [(parsed_server.username, userless_server, token)])
        else:
            logging.warn(
                "specifying a user without a token is unlikely to work")
    else:
        if token:
            logging.warn(
                "specifying a token without a user is probably useless")

    srv = AuthenticatingServerProxy(
        server, allow_none=True, use_datetime=True, auth_backend=auth_backend)
    if server.endswith("xml-rpc/"):
        logging.warn("Please use RPC2 endpoint instead, xml-rpc is deprecated!!!")
        dashboard = srv
    elif server.endswith("RPC2/"):
        #include lava-server/RPC2/
        dashboard = srv.dashboard
    else:
        logging.warn("The url seems not RPC2 or xml-rpc endpoints, please make sure it's a valid one!!!")
        dashboard = srv.dashboard

    logging.debug("server RPC endpoint URL: %s" % server)
    return dashboard


class cmd_submit_results(BaseAction):

    parameters_schema = {
        'type': 'object',
        'properties': {
            'server': {'type': 'string'},
            'stream': {'type': 'string'},
            'result_disk': {'type': 'string', 'optional': True},
            'token': {'type': 'string', 'optional': True},
            },
        'additionalProperties': False,
        }

    def _get_bundles(self, files):
        bundles = []
        errors = []
        for fname in files:
            if os.path.splitext(fname)[1] != ".bundle":
                continue
            content = None
            try:
                with open(fname, 'r') as f:
                    doc = DocumentIO.load(f)[1]
                DocumentEvolution.evolve_document(doc)
                bundles.append(doc)
            except ValueError:
                msg = 'Error adding result bundle %s' % fname
                errors.append(msg)
                logging.exception(msg)
                if content:
                    logging.info('Adding bundle as attachment')
                    attachment = create_attachment(fname, content)
                    self.context.test_data.add_attachments([attachment])
            except:
                msg = 'Unknown error processing bundle' % fname
                logging.exception(msg)
                errors.append(msg)

        if len(errors) > 0:
            msg = ' '.join(errors)
            raise GatherResultsError(msg, bundles)
        return bundles

    def _get_bundles_from_device(self, result_disk):
        bundles = []
        try:
            result_path = self.client.retrieve_results(result_disk)
            if result_path is not None:
                d = tempfile.mkdtemp(dir=self.client.target_device.scratch_dir)
                files = utils.extract_targz(result_path, d)
                bundles = self._get_bundles(files)
        except GatherResultsError:
            raise
        except:
            msg = 'unable to retrieve results from target'
            logging.exception(msg)
            raise GatherResultsError(msg)
        return bundles

    def _get_results_from_host(self):
        bundles = []
        errors = []
        try:
            bundle_list = os.listdir(self.context.host_result_dir)
            for bundle_name in bundle_list:
                bundle = "%s/%s" % (self.context.host_result_dir, bundle_name)
                content = None
                try:
                    with open(bundle) as f:
                        doc = DocumentIO.load(f)[1]
                    DocumentEvolution.evolve_document(doc)
                    bundles.append(doc)
                except ValueError:
                    msg = 'Error adding host result bundle %s' % bundle
                    errors.append(msg)
                    logging.exception(msg)
                    if content:
                        logging.info('Adding bundle as attachment')
                        attachment = create_attachment(bundle, content)
                        self.context.test_data.add_attachments([attachment])
        except:
            msg = 'Error getting all results from host'
            logging.exception(msg)
            raise GatherResultsError(msg, bundles)

        if len(errors) > 0:
            msg = ' '.join(errors)
            raise GatherResultsError(msg, bundles)

        return bundles

    def run(self, server, stream, result_disk="testrootfs", token=None):
        all_bundles = []
        status = 'pass'
        err_msg = ''
        if self.context.any_device_bundles:
            try:
                bundles = self._get_bundles_from_device(result_disk)
                all_bundles.extend(bundles)
            except GatherResultsError as gre:
                err_msg = gre.message
                status = 'fail'
                all_bundles.extend(gre.bundles)
        if self.context.any_host_bundles:
            try:
                bundles = self._get_results_from_host()
                all_bundles.extend(bundles)
            except GatherResultsError as gre:
                err_msg += ' ' + gre.message
                status = 'fail'
                all_bundles.extend(gre.bundles)

        self.context.test_data.add_result('gather_results', status, err_msg)

        main_bundle = self.combine_bundles(all_bundles)

        self.submit_bundle(main_bundle, server, stream, token)

    def combine_bundles(self, all_bundles):
        if not all_bundles:
            main_bundle = {
                     "test_runs": [],
                     "format": "Dashboard Bundle Format 1.6"
                   }
        else:
            main_bundle = all_bundles.pop(0)
            test_runs = main_bundle['test_runs']
            for bundle in all_bundles:
                test_runs += bundle['test_runs']

        attachments = self.client.get_test_data_attachments()
        self.context.test_data.add_attachments(attachments)

        main_bundle['test_runs'].append(self.context.test_data.get_test_run())

        for test_run in main_bundle['test_runs']:
            attributes = test_run.get('attributes', {})
            attributes.update(self.context.test_data.get_metadata())
            test_run['attributes'] = attributes

        return main_bundle

    def submit_bundle(self, main_bundle, server, stream, token):
        dashboard = _get_dashboard(server, token)
        json_bundle = DocumentIO.dumps(main_bundle)
        job_name = self.context.job_data.get('job_name', "LAVA Results")
        try:
            result = dashboard.put_ex(json_bundle, job_name, stream)
            print >> self.context.oob_file, 'dashboard-put-result:', result
            self.context.output.write_named_data('result-bundle', result)
            logging.info("Dashboard : %s" % result)
        except xmlrpclib.Fault, err:
            logging.warning("xmlrpclib.Fault occurred")
            logging.warning("Fault code: %d" % err.faultCode)
            logging.warning("Fault string: %s" % err.faultString)
            raise OperationFailed("could not push to dashboard")


class cmd_submit_results_on_host(cmd_submit_results):
    pass
