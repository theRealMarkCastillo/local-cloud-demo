from urllib.parse import urlparse
import psycopg
from psycopg.rows import dict_row
from redis import Redis
from minio import Minio
from app import settings as s

redis = Redis.from_url(s.REDIS_URL, decode_responses=True, socket_connect_timeout=5, socket_timeout=5)

def db():
    return psycopg.connect(s.DATABASE_URL, row_factory=dict_row, connect_timeout=10)

def storage():
    endpoint = urlparse(s.S3_ENDPOINT)
    return Minio(endpoint.netloc, access_key=s.S3_ACCESS_KEY, secret_key=s.S3_SECRET_KEY,
                 secure=endpoint.scheme == "https", region=s.S3_REGION)
