BOT_TOKEN = "8625419072:AAGVUi2zX05h-TNwJYavXWhU0YSP6GNRKbU"

MYSQL_USER = "2ewpv52LDgvq7sn.root"
MYSQL_PASSWORD = "DygzaBiS7FtcfKzX"
MYSQL_HOST = "gateway01.ap-southeast-1.prod.aws.tidbcloud.com"
MYSQL_PORT = 4000
MYSQL_DB = "telegram_shop"

ADMIN_IDS = [
    7943742895
]


# ==========================================================
# DEPOSIT NETWORKS
# ==========================================================
# NOTE: TRC20 and Binance Spot API have been removed and must
# not be reintroduced. Currently supported: USDT BEP20, USDT Polygon.

USDT_BEP20_CONTRACT = (
    "0x55d398326f99059fF775485246999027B3197955"
)

USDT_POLYGON_CONTRACT = (
    "0xc2132D05D31c914a87C6611C10748AEb04B58e8F"
)

BEP20_ADDRESS = "0x1c5472657c612ff84d7d61b0a60958ccedf85b3d"

# TODO: replace with your actual Polygon receiving wallet address.
POLYGON_ADDRESS = "0x1c5472657c612ff84d7d61b0a60958ccedf85b3d"

# Optional overrides — deposit_checker.py falls back to public RPCs
# if these are not set, so it's safe to leave them out.
# BSC_RPC_URL = "https://bsc-dataseed.binance.org/"
# POLYGON_RPC_URL = "https://polygon-rpc.com/"

# Deposits are verified via Etherscan's unified V2 API
# (https://api.etherscan.io/v2/api), which now covers BSC, Polygon, and
# other chains through a single host + single API key, selected per-call
# via the "chainid" parameter (56 = BSC, 137 = Polygon). This replaced
# the old separate bscscan.com/polygonscan.com hosts/keys.
#
# Get a key at https://etherscan.io/myapikey — the same key that used to
# work at bscscan.com also works here, since BscScan's key was already
# an Etherscan-family key.
ETHERSCAN_API_KEY = "MZ6QY2JX9I6NNM8FWGGMUFBV4ZW1HFJWX7"
