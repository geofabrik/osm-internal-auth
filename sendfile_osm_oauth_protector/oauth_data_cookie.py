import datetime
import base64
import urllib.parse
from http.cookies import SimpleCookie
import requests
from requests_oauthlib import OAuth1, OAuth1Session
import nacl.exceptions

from sendfile_osm_oauth_protector.authentication_state import AuthenticationState
from sendfile_osm_oauth_protector.key_manager import KeyManager


class OAuthDataCookie:
    def __init__(self, config, environ, key_manager):
        """
        Args:
            config (Config): configuration
            environ (Dictionary): contains CGI environment variables (see PEP 0333)
            key_manager (KeyManager): key store holding keys for encryption and signatures
        """
        self.config = config
        self.read_cookie(environ)
        self.query_params =  urllib.parse.parse_qs(environ["QUERY_STRING"])
        self.key_manager = key_manager
        self.read_crypto_box = None
        self.write_crypto_box = self.key_manager.boxes[config.KEY_NAME]
        self.verify_key = None
        self.sign_key = self.key_manager.signing_keys[config.KEY_NAME]
        self.access_token = ""
        self.access_token_secret = ""
        self.valid_until = datetime.datetime.utcnow() - config.AUTH_TIMEOUT

    def _load_read_keys(self, key_name):
        """
        Fetch the keys for decrypting and verification of the cookie provided by the client.

        Args:
            key_name (str): name of the key to look up in the key manager
        """
        self.read_crypto_box = self.key_manager.boxes[key_name]
        self.verify_key = self.key_manager.verify_keys[key_name]

    def get_access_token_from_api(self):
        """
        Retriev an access_token and an access_token_secret from the OSM API
        using a temporary oauth_token and oauth_token_secret.

        The resulting tokens will be saved as properties of this class.
        """
        oauth_token = self.query_params["oauth_token"][0]
        oauth_token_secret_encr = self.query_params["oauth_token_secret_encr"][0]
        oauth_token_secret = self.write_crypto_box.decrypt(base64.urlsafe_b64decode(oauth_token_secret_encr))
        oauth = OAuth1Session(self.config.CLIENT_KEY, client_secret=self.config.CLIENT_SECRET, resource_owner_key=oauth_token,
                              resource_owner_secret=oauth_token_secret)
        #TODO catch exceptions (network)
        oauth_tokens = oauth.fetch_access_token(self.config.ACCESS_TOKEN_URL)
        self.access_token = oauth_tokens.get("oauth_token")
        self.access_token_secret = oauth_tokens.get("oauth_token_secret")

    def get_state(self):
        """
        Check if the signature of the cookie is valid, decrypt the cookie.

        Returns:
            AuthenticationState
        """
        ITERATION2_KEYS = {"oauth_token", "oauth_token_secret_encr"}
        if (ITERATION2_KEYS & set(iter(self.query_params))) == set(ITERATION2_KEYS):
            return AuthenticationState.LOGGED_IN
        if self.cookie is None:
            return AuthenticationState.NONE
        try:
            contents = self.cookie[self.config.COOKIE_NAME].value.split("|")
        except KeyError:
            return AuthenticationState.NONE
        if len(contents) < 3 or contents[0] == "logout" or contents[0] != "login":
            return AuthenticationState.NONE
        try:
            key_name = contents[1]
            self._load_read_keys(key_name)
        except KeyError:
            return AuthenticationState.NONE
        signed = contents[2].encode("ascii")
        try:
            access_tokens_encr = self.verify_key.verify(base64.urlsafe_b64decode(signed))
        except nacl.exceptions.BadSignatureError:
            return AuthenticationState.SIGNATURE_VERIFICATION_FAILED
        except KeyError:
            return AuthenticationState.NONE
        try:
            parts = self.read_crypto_box.decrypt(access_tokens_encr).decode("ascii").split("|")
            self.access_token = parts[0]
            self.access_token_secret = parts[1]
            self.valid_until = datetime.datetime.strptime(parts[2], "%Y-%m-%dT%H:%M:%S")
        except:
            return AuthenticationState.OAUTH_TOKEN_DECRYPTION_FAILED
        if datetime.datetime.utcnow() > self.valid_until:
            return AuthenticationState.OAUTH_ACCESS_TOKEN_RECHECK
        return AuthenticationState.OAUTH_ACCESS_TOKEN_VALID

    def check_with_osm_api(self):
        """
        Initiate checking of the authorization and reset the validity of the
        cookie if the check passed.

        Returns:
            boolean: result of _check_with_osm_api()
        """
        if not self._check_with_osm_api():
            return False
        self.valid_until = datetime.datetime.utcnow() + self.config.AUTH_TIMEOUT
        return True

    def _check_with_osm_api(self):
        """
        Recheck the authorization by requesting a protected resource from the OSM API.

        Returns:
            boolean: True if the source could be request, False if the request
                     failed (repsonse code other than 200)
        """
        oauth = OAuth1(self.config.CLIENT_KEY, client_secret=self.config.CLIENT_SECRET, resource_owner_key=self.access_token,
                       resource_owner_secret=self.access_token_secret)
        r = requests.get(url="https://api.openstreetmap.org/api/0.6/user/details", auth=oauth)
        if r.status_code == 200:
            return True
        return False

    def output(self):
        """
        Return an instance of http.cookies.SimpleCookie.

        See doc/cookie.md for a description of the contents of the cookie.

        This method concatenates the access token, access token secret and date
        when the next full check has to be done. This concatenated string is
        encrypted, signed and handed over to _output_cookie() whose result will
        be returned.
        """
        nonce = nacl.utils.random(nacl.public.Box.NONCE_SIZE)
        valid_until = self.valid_until.strftime("%Y-%m-%dT%H:%M:%S")
        tokens = "{}|{}|{}".format(self.access_token, self.access_token_secret, valid_until)
        access_tokens_encr = self.write_crypto_box.encrypt(tokens.encode("ascii"), nonce)
        access_tokens_encr_signed = base64.urlsafe_b64encode(self.sign_key.sign(access_tokens_encr)).decode("ascii")
        return self._output_cookie(True, access_tokens_encr_signed)

    def _output_cookie(self, logged_in, encrypted_signed_tokens=None):
        """
        Return an instance of http.cookies.SimpleCookie.

        See doc/cookie.md for a description of the contents of the cookie.

        Args:
            logged_in (boolean): if the user is logged in (successfully
                                 authenticated)
            encrypted_signed_tokens (str): encrypted and signed concatenation
                                           of the access token, access token
                                           secret and date of the next full
                                           verification

        Returns:
            http.cookies.SimpleCookie: the cookie
        """
        cookie = SimpleCookie()
        if logged_in:
            cookie[self.config.COOKIE_NAME] = "login|{}|{}".format(self.config.KEY_NAME, encrypted_signed_tokens)
        else:
            cookie[self.config.COOKIE_NAME] = "logout||"
        cookie[self.config.COOKIE_NAME]["httponly"] = True
        if self.config.COOKIE_SECURE:
            cookie[self.config.COOKIE_NAME]["secure"] = True
        return cookie[self.config.COOKIE_NAME].OutputString()

    def logout_cookie(self):
        """
        Return a cookie for a logged out user.

        Returns:
            http.cookies.SimpleCookie: the cookie
        """
        return self._output_cookie(False)

    def read_cookie(self, environ):
        """
        Read cookies from the enviroment variables.

        Args:
            environ (Dictionary): contains CGI environment variables (see PEP 0333)

        Returns:
            http.cookies.SimpleCookie: successfully read cookie, None otherwise
        """
        self.cookie = None
        if "HTTP_COOKIE" in environ:
            cookie = SimpleCookie(environ["HTTP_COOKIE"])
            if self.config.COOKIE_NAME in cookie:
                self.cookie = cookie
