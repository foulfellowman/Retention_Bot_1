from twilio.request_validator import RequestValidator
from twilio.rest import Client


class TwilioSMSClient:
    def __init__(self, account_sid: str, auth_token: str, messaging_sid: str):
        self._account_sid = account_sid
        self._auth_token = auth_token
        self._messaging_sid = messaging_sid
        self._client = Client(account_sid, auth_token)
        self._validator = RequestValidator(auth_token) if auth_token else None

    def send_sms(self, to_phone: str, message: str):
        twilio_message = self._client.messages.create(
            messaging_service_sid=self._messaging_sid,
            to=to_phone,
            body=message
        )
        return getattr(twilio_message, "sid", None)

    def validate_webhook(self, signature: str | None, url: str, params: dict | None) -> bool:
        """
        Verify that an incoming webhook genuinely originated from Twilio.
        """
        if not self._auth_token:
            raise RuntimeError("Twilio auth token is required for webhook validation.")
        if self._validator is None:
            self._validator = RequestValidator(self._auth_token)
        if not signature:
            return False
        payload = params or {}
        return bool(self._validator.validate(url, payload, signature))

    def verify_credentials(self) -> None:
        if not (self._account_sid and self._auth_token):
            raise RuntimeError("Twilio credentials are not fully configured.")
        # Fetch account data to ensure the SID/token pair is usable
        self._client.api.accounts(self._account_sid).fetch()

    def get_client(self):
        return self._client

    def get_sid(self):
        return self._account_sid

    def set_sid(self, new_sid: str):
        self._account_sid = new_sid
        self._client = Client(self._account_sid, self._auth_token)

    def get_token(self):
        return self._auth_token

    def set_token(self, new_token: str):
        self._auth_token = new_token
        self._client = Client(self._account_sid, self._auth_token)
        self._validator = RequestValidator(new_token) if new_token else None
