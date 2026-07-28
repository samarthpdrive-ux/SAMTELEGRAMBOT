BOT_TOKEN = "8625419072:AAGVUi2zX05h-TNwJYavXWhU0YSP6GNRKbU"
# ==========================
# MySQL / TiDB Cloud
# ==========================
MYSQL_HOST = "gateway01.ap-southeast-1.prod.aws.tidbcloud.com"
MYSQL_PORT = 4000
MYSQL_USER = "2ewpv52LDgvq7sn.root"
MYSQL_PASSWORD = "DygzaBiS7FtcfKzX"
MYSQL_DB = "telegram_shop"
# Change this if your certificate has a different filename
MYSQL_SSL_CA = "ca.pem"
ADMIN_IDS = [
    8790675033
]
# ==========================================================
# DEPOSIT NETWORKS
# ==========================================================
USDT_BEP20_CONTRACT = (
    "0x55d398326f99059fF775485246999027B3197955"
)
USDT_POLYGON_CONTRACT = (
    "0xc2132D05D31c914a87C6611C10748AEb04B58e8F"
)
BEP20_ADDRESS = "0x1c5472657c612ff84d7d61b0a60958ccedf85b3d"
POLYGON_ADDRESS = "0x1c5472657c612ff84d7d61b0a60958ccedf85b3d"
# Optional overrides
# BSC_RPC_URL = "https://bsc-dataseed.binance.org/"
# POLYGON_RPC_URL = "https://polygon-rpc.com/"
ETHERSCAN_API_KEY = "MZ6QY2JX9I6NNM8FWGGMUFBV4ZW1HFJWX7"

# ==========================================================
# DEPOSIT — UPI (Gmail IMAP + UTR matching via FamApp emails)
# ==========================================================
UPI_ID = "samarthp2727@fam"  # TODO: replace with your real UPI ID (VPA)

IMAP_HOST = "imap.gmail.com"
IMAP_EMAIL = "samarthpdrive@gmail.com"

IMAP_APP_PASSWORD = "ynrmnixopucqjjqz"  # TODO: your 16-character Gmail App Password

# TODO: this is a placeholder guess. Open a real FamApp payment email
# in Gmail -> "Show original" -> copy the exact From: address here.
FAMAPP_SENDER_EMAIL = "no-reply@famapp.in"

IMAP_LOOKBACK_DAYS = 1
