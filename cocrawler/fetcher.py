'''
async fetching of urls.

Assumes robots checks have already been done.

Success returns response object and response bytes (which were already
read in order to shake out all potential network-related exceptions.)

Failure returns enough details for the caller to do something smart:
503, other 5xx, DNS fail, connect timeout, error between connect and
full response, proxy failure. Plus an errorstring good enough for logging.

'''

import time
import traceback
from collections import namedtuple
import ssl
import urllib

import asyncio
import logging
import aiohttp

from . import stats
from . import config
from . import content

LOGGER = logging.getLogger(__name__)

# these errors get printed deep in aiohttp but they also bubble up
aiohttp_errors = {
    'SSL handshake failed',
    'SSL error errno:1 reason: CERTIFICATE_VERIFY_FAILED',
    'SSL handshake failed on verifying the certificate',
    'Fatal error on transport TCPTransport',
    'Fatal error on SSL transport',
    'SSL error errno:1 reason: UNKNOWN_PROTOCOL',
    'Future exception was never retrieved',
    'Unclosed connection',
    'SSL error errno:1 reason: TLSV1_UNRECOGNIZED_NAME',
    'SSL error errno:1 reason: SSLV3_ALERT_HANDSHAKE_FAILURE',
    'SSL error errno:1 reason: TLSV1_ALERT_INTERNAL_ERROR',
}


class AsyncioSSLFilter(logging.Filter):
    def filter(self, record):
        if record.name == 'asyncio' and record.levelname == 'ERROR':
            msg = record.getMessage()
            for ae in aiohttp_errors:
                if msg.startswith(ae):
                    return False
        return True


def establish_filters():
    f = AsyncioSSLFilter()
    logging.getLogger('asyncio').addFilter(f)


# XXX should be a policy plugin
# XXX cookie handling -- can be per-get -- make per-domain jar
def apply_url_policies(url, crawler):
    headers = {}

    headers['User-Agent'] = crawler.ua

    if crawler.prevent_compression:
        headers['Accept-Encoding'] = 'identity'
    else:
        headers['Accept-Encoding'] = content.get_accept_encoding()

    if crawler.upgrade_insecure_requests:
        headers['Upgrade-Insecure-Requests'] = '1'

    proxy, prefetch_dns = global_policies()

    return headers, proxy, prefetch_dns


def global_policies():
    proxy = config.read('Fetcher', 'ProxyAll')
    prefetch_dns = not proxy or config.read('GeoIP', 'ProxyGeoIP')

    return proxy, prefetch_dns


FetcherResponse = namedtuple('FetcherResponse', ['response', 'body_bytes', 'ip', 'req_headers',
                                                 't_first_byte', 't_last_byte', 'is_truncated',
                                                 'last_exception'])


async def fetch(url, session, headers=None, proxy=None,
                allow_redirects=None, max_redirects=None,
                stats_prefix='', max_page_size=-1):

    last_exception = None
    is_truncated = False

    try:
        t0 = time.time()
        last_exception = None
        body_bytes = b''
        blocks = []
        left = max_page_size
        ip = None

        with stats.coroutine_state(stats_prefix+'fetcher fetching'):
            with stats.record_latency(stats_prefix+'fetcher fetching', url=url.url):
                response = await session.get(url.url,
                                             allow_redirects=allow_redirects,
                                             max_redirects=max_redirects,
                                             proxy=proxy, headers=headers)

                # https://aiohttp.readthedocs.io/en/stable/tracing_reference.html
                # XXX should use tracing events to get t_first_byte
                t_first_byte = '{:.3f}'.format(time.time() - t0)

                if not proxy:
                    try:
                        ip, _ = response.connection.transport.get_extra_info('peername', default=None)
                    except AttributeError:
                        pass

                while left > 0:
                    block = await response.content.read(left)
                    if not block:
                        body_bytes = b''.join(blocks)
                        break
                    blocks.append(block)
                    left -= len(block)
                else:
                    body_bytes = b''.join(blocks)

                if not response.content.at_eof():
                    stats.stats_sum(stats_prefix+'fetch truncated length', 1)
                    response.close()  # this does interrupt the network transfer
                    is_truncated = 'length'  # testme WARC

                t_last_byte = '{:.3f}'.format(time.time() - t0)
    except asyncio.TimeoutError as e:
        stats.stats_sum(stats_prefix+'fetch timeout', 1)
        last_exception = 'TimeoutError'
        body_bytes = b''.join(blocks)
        if len(body_bytes):
            is_truncated = 'time'  # testme WARC
            stats.stats_sum(stats_prefix+'fetch timeout body bytes found', 1)
            stats.stats_sum(stats_prefix+'fetch timeout body bytes found bytes', len(body_bytes))
    except (aiohttp.ClientError) as e:
        # ClientError is a catchall for a bunch of things
        # e.g. DNS errors, '400' errors for http parser errors
        # ClientConnectorCertificateError for an SSL cert that doesn't match hostname
        # ClientConnectorError(None, None) caused by robots redir to DNS fail
        # ServerDisconnectedError(None,) caused by servers that return 0 bytes for robots.txt fetches
        # TooManyRedirects("0, message=''",) caused by too many robots.txt redirs 
        stats.stats_sum(stats_prefix+'fetch ClientError', 1)
        detailed_name = str(type(e).__name__)
        last_exception = 'ClientError: ' + detailed_name + ': ' + str(e)
        body_bytes = b''.join(blocks)
        if len(body_bytes):
            is_truncated = 'disconnect'  # testme WARC
            stats.stats_sum(stats_prefix+'fetch ClientError body bytes found', 1)
            stats.stats_sum(stats_prefix+'fetch ClientError body bytes found bytes', len(body_bytes))
    except ssl.CertificateError as e:
        # unfortunately many ssl errors raise and have tracebacks printed deep in aiohttp
        # so this doesn't go off much
        stats.stats_sum(stats_prefix+'fetch SSL error', 1)
        last_exception = 'CertificateError: ' + str(e)
    except ValueError as e:
        # no A records found -- raised by our dns code
        # aiohttp raises:
        # ValueError Location: https:/// 'Host could not be detected' -- robots fetch
        # ValueError Location: http:// /URL should be absolute/ -- robots fetch
        # ValueError 'Can redirect only to http or https' -- robots fetch -- looked OK to curl!
        stats.stats_sum(stats_prefix+'fetch other error - ValueError', 1)
        last_exception = 'ValueErorr: ' + str(e)
    except AttributeError as e:
        stats.stats_sum(stats_prefix+'fetch other error - AttributeError', 1)
        last_exception = 'AttributeError: ' + str(e)
    except RuntimeError as e:
        stats.stats_sum(stats_prefix+'fetch other error - RuntimeError', 1)
        last_exception = 'RuntimeError: ' + str(e)
    except asyncio.CancelledError:
        raise
    except Exception as e:
        last_exception = 'Exception: ' + str(e)
        stats.stats_sum(stats_prefix+'fetch surprising error', 1)
        LOGGER.info('Saw surprising exception in fetcher working on %s:\n%s', url.url, last_exception)
        traceback.print_exc()

    if last_exception is not None:
        LOGGER.info('we failed working on %s, the last exception is %s', url.url, last_exception)
        return FetcherResponse(None, None, None, None, None, None, False, last_exception)

    fr = FetcherResponse(response, body_bytes, ip, response.request_info.headers,
                         t_first_byte, t_last_byte, is_truncated, None)

    if response.status >= 500:
        LOGGER.debug('server returned http status %d', response.status)

    stats.stats_sum(stats_prefix+'fetch bytes', len(body_bytes) + len(response.raw_headers))

    stats.stats_sum(stats_prefix+'fetch URLs', 1)
    stats.stats_sum(stats_prefix+'fetch http code=' + str(response.status), 1)

    # checks after fetch:
    # hsts header?
    # if ssl, check strict-transport-security header, remember max-age=foo part., other stuff like includeSubDomains
    # did we receive cookies? was the security bit set?

    return fr


def upgrade_scheme(url):
    '''
    Upgrade crawled scheme to https, if reasonable. This helps to reduce MITM attacks against the crawler.

    https://chromium.googlesource.com/chromium/src/net/+/master/http/transport_security_state_static.json

    Alternately, the return headers from a site might have strict-transport-security set ... a bit more
    dangerous as we'd have to respect the timeout to avoid permanently learning something that's broken

    TODO: use HTTPSEverwhere? would have to have a fallback if https failed, which it occasionally will
    '''
    return url
