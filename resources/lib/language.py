from slyguy.language import BaseLanguage

class Language(BaseLanguage):
    LOGIN = 30000
    LOGOUT = 30001
    LIVE_STREAMS = 30002
    SEARCH = 30003
    LOGIN_ERROR = 30006
    ASK_EMAIL = 30007
    ASK_PASSWORD = 30008
    LOGOUT_YES_NO = 30009
    WATCHLIST = 30010
    CONTINUE_WATCHING = 30011
    LOGIN_SUCCESS = 30012
    ACCOUNT_EXPIRED = 30013

_ = Language()
