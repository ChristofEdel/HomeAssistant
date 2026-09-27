

DOMAIN = "simple_influxdb"


BATCH_BUFFER_SIZE = 100             # How many updates we can buffer together in a single
                                    # InfluxDB request before sending
QUEUE_BACKLOG_SECONDS = 30

BATCH_TIMEOUT = 1                   # Nmber of seconds we wait for a batch to be accepted by InfluxDB
RETRY_DELAY = 20


CONF_SSL_CA_CERT = "ssl_ca_cert"
CONF_MAX_RETRIES="max_retries"
CONF_INCLUDE="include"
CONF_EXCLUDE="exclude"
CONF_EXCLUDE_UNRECORDED="exclude_unrecorded"
CONF_PRECISION = "precision"
CONF_BUCKET = "bucket"

TIMEOUT = 10  # seconds