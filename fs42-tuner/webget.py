"""Open an http or https address, and nothing else.

urllib's default opener also reads file:, ftp: and data: addresses, so a plain urlopen() on an address that came from a
setting or a playlist could be made to read a local file. This opener is built from the http and https handlers
only (plus the redirect and error handling they need): any other scheme, in the address or in a redirect, is
refused with URLError.
"""
import urllib.error
import urllib.request

_opener = urllib.request.OpenerDirector()
for _handler in (urllib.request.HTTPHandler, urllib.request.HTTPSHandler, urllib.request.HTTPRedirectHandler,
                 urllib.request.HTTPDefaultErrorHandler, urllib.request.HTTPErrorProcessor,
                 urllib.request.UnknownHandler):
    _opener.add_handler(_handler())


def open_url(request, timeout=30):
    """Like urllib.request.urlopen(request, timeout=...), for http and https addresses only."""
    return _opener.open(request, timeout=timeout)
