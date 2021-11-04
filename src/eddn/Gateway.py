# coding: utf8

"""
EDDN Gateway, which receives message from uploaders.

Contains the necessary ZeroMQ socket and a helper function to publish
market data to the Announcer daemons.
"""
import argparse
import hashlib
import logging
import zlib
from datetime import datetime
from typing import Dict
from urllib.parse import parse_qs

import gevent
import simplejson
import zmq.green as zmq
from bottle import Bottle, request, response
from gevent import monkey
from pkg_resources import resource_string

from eddn.conf.Settings import Settings, loadConfig
from eddn.core.Validator import ValidationSeverity, Validator

monkey.patch_all()
import bottle
from bottle import Bottle, request, response
bottle.BaseRequest.MEMFILE_MAX = 1024 * 1024 # 1MiB, default is/was 100KiB

app = Bottle()

logger = logging.getLogger(__name__)
logger.setLevel(logging.INFO)
__logger_channel = logging.StreamHandler()
__logger_formatter = logging.Formatter(
    '%(asctime)s - %(levelname)s - %(module)s:%(lineno)d: %(message)s'
)
__logger_formatter.default_time_format = '%Y-%m-%d %H:%M:%S'
__logger_formatter.default_msec_format = '%s.%03d'
__logger_channel.setFormatter(__logger_formatter)
logger.addHandler(__logger_channel)
logger.info('Made logger')


# This socket is used to push market data out to the Announcers over ZeroMQ.
zmq_context = zmq.Context()
sender = zmq_context.socket(zmq.PUB)

validator = Validator()

# This import must be done post-monkey-patching!
from eddn.core.StatsCollector import StatsCollector  # noqa: E402

stats_collector = StatsCollector()
stats_collector.start()


def parse_cl_args():
    parser = argparse.ArgumentParser(
        prog='Gateway',
        description='EDDN Gateway server',
    )

    parser.add_argument(
        '--loglevel',
        help='Logging level to output at',
    )

    parser.add_argument(
        '-c', '--config',
        metavar='config filename',
        nargs='?',
        default=None,
    )

    return parser.parse_args()


def extract_message_details(parsed_message):
    uploader_id = '<<UNKNOWN>>'
    software_name = '<<UNKNOWN>>'
    software_version = '<<UNKNOWN>>'
    schema_ref = '<<UNKNOWN>>'
    journal_event = '<<UNKNOWN>>'

    if 'header' in parsed_message:
        if 'uploaderID' in parsed_message['header']:
            uploader_id = parsed_message['header']['uploaderID']

        if 'softwareName' in parsed_message['header']:
            software_name = parsed_message['header']['softwareName']

        if 'softwareVersion' in parsed_message['header']:
            software_version = parsed_message['header']['softwareVersion']

    if '$schemaRef' in parsed_message:
        schema_ref = parsed_message['$schemaRef']


        if '/journal/' in schema_ref:
            if 'message' in parsed_message:
                if 'event' in parsed_message['message']:
                    journal_event = parsed_message['message']['event']

        else:
            journal_event = '-'

    return uploader_id, software_name, software_version, schema_ref, journal_event

def configure() -> None:
    """
    Get the list of transports to bind from settings.

    This allows us to PUB messages to multiple announcers over a variety of
    socket types (UNIX sockets and/or TCP sockets).
    """
    for binding in Settings.GATEWAY_SENDER_BINDINGS:
        sender.bind(binding)

    for schema_ref, schema_file in Settings.GATEWAY_JSON_SCHEMAS.iteritems():
        validator.addSchemaResource(schema_ref, resource_string('eddn.Gateway', schema_file))


def push_message(parsed_message: Dict, topic: str) -> None:
    """
    Push a message our to subscribed listeners.

    Spawned as a greenlet to push messages (strings) through ZeroMQ.
    This is a dumb method that just pushes strings; it assumes you've already
    validated and serialised as you want to.
    """
    string_message = simplejson.dumps(parsed_message, ensure_ascii=False).encode('utf-8')

    # Push a zlib compressed JSON representation of the message to
    # announcers with schema as topic
    compressed_msg = zlib.compress(string_message)

    send_message = f"{str(topic)!r} |-| {compressed_msg!r}"

    sender.send(send_message)
    stats_collector.tally("outbound")


def get_remote_address() -> str:
    """
    Determine the address of the uploading client.

    First checks the for proxy-forwarded headers, then falls back to
    request.remote_addr.
    :returns: Best attempt at remote address.
    """
    return request.headers.get('X-Forwarded-For', request.remote_addr)


def get_decompressed_message() -> bytes:
    """
    Detect gzip Content-Encoding headers and de-compress on the fly.

    For upload formats that support it.
    :rtype: str
    :returns: The de-compressed request body.
    """
    content_encoding = request.headers.get('Content-Encoding', '')
    logger.debug('Content-Encoding: ' + content_encoding)

    if content_encoding in ['gzip', 'deflate']:
        logger.debug('Content-Encoding of gzip or deflate...')
        # Compressed request. We have to decompress the body, then figure out
        # if it's form-encoded.
        try:
            # Auto header checking.
            logger.debug('Trying zlib.decompress (15 + 32)...')
            message_body = zlib.decompress(request.body.read(), 15 + 32)

        except zlib.error:
            logger.error('zlib.error, trying zlib.decompress (-15)')
            # Negative wbits suppresses adler32 checksumming.
            message_body = zlib.decompress(request.body.read(), -15)
            logger.debug('Resulting message_body:\n%s\n' % (message_body))

        # At this point, we're not sure whether we're dealing with a straight
        # un-encoded POST body, or a form-encoded POST. Attempt to parse the
        # body. If it's not form-encoded, this will return an empty dict.
        form_enc_parsed = parse_qs(message_body)
        if form_enc_parsed:
            logger.info('Request is form-encoded, compressed, from %s' % (get_remote_address()))
            # This is a form-encoded POST. The value of the data attrib will
            # be the body we're looking for.
            try:
                message_body = form_enc_parsed[b'data'][0]

            except (KeyError, IndexError):
                logger.error('form-encoded, compressed, upload did not contain a "data" key. From %s', get_remote_address())
                raise MalformedUploadError(
                    "No 'data' POST key/value found. Check your POST key "
                    "name for spelling, and make sure you're passing a value."
                )

        else:
            logger.debug('Request is *NOT* form-encoded')

    else:
        logger.debug('Content-Encoding indicates *not* compressed...')

        # Uncompressed request. Bottle handles all of the parsing of the
        # POST key/vals, or un-encoded body.
        data_key = request.forms.get('data')
        if data_key:
            logger.info('Request is form-encoded, uncompressed, from %s' % (get_remote_address()))
            # This is a form-encoded POST. Support the silly people.
            message_body = data_key

        else:
            logger.debug('Plain POST request detected...')
            # This is a non form-encoded POST body.
            message_body = request.body.read()

    return message_body


def parse_and_error_handle(data: bytes) -> str:
    """
    Parse an incoming message and handle errors.

    :param data:
    :return: The decoded message, or an error message.
    """
    try:
        parsed_message = simplejson.loads(data)

    except (MalformedUploadError, TypeError, ValueError) as exc:
        # Something bad happened. We know this will return at least a
        # semi-useful error message, so do so.
        try:
            logger.error('Error - JSON parse failed (%d, "%s", "%s", "%s", "%s", "%s") from %s:\n%s\n' % (
                    request.content_length,
                    '<<UNKNOWN>>',
                    '<<UNKNOWN>>',
                    '<<UNKNOWN>>',
                    '<<UNKNOWN>>',
                    '<<UNKNOWN>>',
                    get_remote_address(),
                    data[:512]
            ))

        except Exception as e:
            print('Logging of "JSON parse failed" failed: %s' % (e.message))
            pass

        response.status = 400
        logger.error(f"Error to {get_remote_address()}: {exc}")
        return str(exc)
        return 'FAIL: JSON parsing: ' + str(exc)

    # Here we check if an outdated schema has been passed
    if parsed_message["$schemaRef"] in Settings.GATEWAY_OUTDATED_SCHEMAS:
        response.status = '426 Upgrade Required'  # Bottle (and underlying httplib) don't know this one
        stats_collector.tally("outdated")
        return "FAIL: Outdated Schema: The schema you have used is no longer supported. Please check for an updated " \
               "version of your application."

    validation_results = validator.validate(parsed_message)

    if validation_results.severity <= ValidationSeverity.WARN:
        parsed_message['header']['gatewayTimestamp'] = datetime.utcnow().isoformat() + 'Z'
        parsed_message['header']['uploaderIP'] = get_remote_address()

        # Sends the parsed message to the Relay/Monitor as compressed JSON.
        gevent.spawn(push_message, parsed_message, parsed_message['$schemaRef'])

        try:
            uploader_id, software_name, software_version, schema_ref, journal_event = extract_message_details(parsed_message)
            logger.info('Accepted (%d, "%s", "%s", "%s", "%s", "%s") from %s' % (
                request.content_length,
                uploader_id, software_name, software_version, schema_ref, journal_event,
                get_remote_address()
            ))

        except Exception as e:
            print('Logging of Accepted request failed: %s' % (e.message))
            pass

        return 'OK'

    else:
        try:
            uploader_id, software_name, software_version, schema_ref, journal_event = extract_message_details(parsed_message)
            logger.error('Failed Validation "%s" (%d, "%s", "%s", "%s", "%s", "%s") from %s' % (
                    str(validationResults.messages),
                    request.content_length,
                    uploader_id, software_name, software_version, schema_ref, journal_event,
                    get_remote_address()
            ))

        except Exception as e:
            print('Logging of Failed Validation failed: %s' % (e.message))
            pass

        response.status = 400
        stats_collector.tally("invalid")
        return "FAIL: Schema Validation: " + str(validationResults.messages)


@app.route('/upload/', method=['OPTIONS', 'POST'])
def upload() -> str:
    """
    Handle an /upload/ request.

    :return: The processed message, else error string.
    """
    try:
        # Body may or may not be compressed.
        message_body = get_decompressed_message()

    except zlib.error as exc:
        # Some languages and libs do a crap job zlib compressing stuff. Provide
        # at least some kind of feedback for them to try to get pointed in
        # the correct direction.
        response.status = 400
        try:
            logger.error(f'gzip error ({request.content_length}, "<<UNKNOWN>>", "<<UNKNOWN>>", "<<UNKNOWN>>", "<<UNKNOWN>>", "<<UNKNOWN>>") from {get_remote_address()}')

        except Exception as e:
            print('Logging of "gzip error" failed: %s' % (e.message))
            pass

        return 'FAIL: zlib.error: ' + exc.message

    except MalformedUploadError as exc:
        # They probably sent an encoded POST, but got the key/val wrong.
        response.status = 400
        logger.error(f"MalformedUploadError from {get_remote_address()}: {exc.message}")

        return 'FAIL: Malformed Upload: ' + exc.message

    stats_collector.tally("inbound")
    return parse_and_error_handle(message_body)


@app.route('/health_check/', method=['OPTIONS', 'GET'])
def health_check() -> str:
    """
    Return our version string in as an 'am I awake' signal.

    This should only be used by the gateway monitoring script. It is used
    to detect whether the gateway is still alive, and whether it should remain
    in the DNS rotation.

    :returns: Version of this software.
    """
    return Settings.EDDN_VERSION


@app.route('/stats/', method=['OPTIONS', 'GET'])
def stats() -> str:
    """
    Return some stats about the Gateway's operation so far.

    :return: JSON stats data
    """
    stats_current = stats_collector.getSummary()
    stats_current["version"] = Settings.EDDN_VERSION
    return simplejson.dumps(stats_current)


class MalformedUploadError(Exception):
    """
    Exception for malformed upload.

    Raise this when an upload is structurally incorrect. This isn't so much
    to do with something like a bogus region ID, this is more like "You are
    missing a POST key/val, or a body".
    """

    pass


class EnableCors(object):
    """Handle enabling CORS headers in all responses."""

    name = 'enable_cors'
    api = 2

    def apply(self, fn, context):
        """
        Apply CORS headers to the calling bottle app.

        :param fn:
        :param context:
        :return:
        """
        def _enable_cors(*args, **kwargs):
            # set CORS headers
            response.headers['Access-Control-Allow-Origin'] = '*'
            response.headers['Access-Control-Allow-Methods'] = 'GET, POST, PUT, OPTIONS'
            response.headers['Access-Control-Allow-Headers'] = \
                'Origin, Accept, Content-Type, X-Requested-With, X-CSRF-Token'

            if request.method != 'OPTIONS':
                # actual request; reply with the actual response
                return fn(*args, **kwargs)

        return _enable_cors


def main() -> None:
    """Handle setting up and running the bottle app."""
    cl_args = parse_cl_args()
    if cl_args.loglevel:
        logger.setLevel(cl_args.loglevel)

    loadConfig(cl_args)
    configure()

    app.install(EnableCors())
    app.run(
        host=Settings.GATEWAY_HTTP_BIND_ADDRESS,
        port=Settings.GATEWAY_HTTP_PORT,
        server='gevent',
        certfile=Settings.CERT_FILE,
        keyfile=Settings.KEY_FILE
    )


if __name__ == '__main__':
    main()
