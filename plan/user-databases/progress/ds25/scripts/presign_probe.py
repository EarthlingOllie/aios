import time

import boto3
import httpx
from botocore.config import Config

kw = dict(
    aws_access_key_id="aios",
    aws_secret_access_key="aios-dev-secret",
    region_name="us-east-1",
    config=Config(signature_version="s3v4", s3={"addressing_style": "path"}),
)
s3 = boto3.client("s3", endpoint_url="http://localhost:9525", **kw)
try:
    s3.create_bucket(Bucket="probe")
except Exception as e:
    print("create", e)
s3.put_object(Bucket="probe", Key="stores/x/documents/1", Body=b"hello \xe2\x9c\x93 bytes")
pub = boto3.client("s3", endpoint_url="http://127.0.0.1:9525", **kw)
url = pub.generate_presigned_url(
    "get_object",
    Params={
        "Bucket": "probe",
        "Key": "stores/x/documents/1",
        "ResponseContentDisposition": "attachment; filename=\"resume 1.pdf\"; "
        "filename*=UTF-8''r%C3%A9sum%C3%A9%201.pdf",
    },
    ExpiresIn=2,
)
print(url)
r = httpx.get(url)
print(r.status_code, r.headers.get("content-disposition"), r.content)
time.sleep(4)
r = httpx.get(url)
print("after expiry", r.status_code, r.text[:300])
bad = url.replace("Signature=", "Signature=0")
print("tampered", httpx.get(bad).status_code)
