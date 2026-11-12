"""Inbound mail: `POST /api/inbound-mail`, called by the deployment's receiving mail service
with each message it received for the inbound mail domain, as JSON, with `inbound_mail_secret`
as its bearer token. No one signs in, and no organisation is in the path: each recipient that's
an email source's address leads to its store (`aios.push_sources`).

- **404** where the deployment has no inbound mail domain;
- **401** for a missing or wrong token;
- **413** past `inbound_mail_body_limit_bytes`; **422** for a body that isn't a message;
- **202** otherwise, with how many sources it reached: a message to no source's address, from a
  sender who isn't allowed, or failing sender authentication is dropped, not refused (the
  receiving service shouldn't retry it).
"""

import hmac

from fastapi import APIRouter, HTTPException, Request, status
from fastapi.concurrency import run_in_threadpool
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from aios import push_sources
from aios.config import get_settings

router = APIRouter(prefix="/api")


class AttachmentIn(BaseModel):
    filename: str = Field("", max_length=255)
    content_type: str | None = Field(None, max_length=255)
    # Base64.
    content: str


class MailIn(BaseModel):
    """A message, as the receiving service reports it."""

    model_config = ConfigDict(populate_by_name=True)

    to: list[str] = Field(min_length=1, max_length=100)
    sender: str = Field(alias="from", max_length=1000)
    subject: str = Field("", max_length=2000)
    message_id: str | None = Field(None, max_length=1000)
    in_reply_to: str | None = Field(None, max_length=1000)
    references: list[str] = Field(default_factory=list, max_length=200)
    text: str = ""
    # The receiving service's sender authentication results: "pass", "fail", "none", ...
    spf: str | None = Field(None, max_length=32)
    dkim: str | None = Field(None, max_length=32)
    attachments: list[AttachmentIn] = Field(default_factory=list, max_length=100)


def _authorised(header: str | None) -> bool:
    expected = get_settings().inbound_mail_secret.get_secret_value()
    given = (header or "").removeprefix("Bearer ").strip()
    return bool(expected) and given.isascii() and hmac.compare_digest(given, expected)


async def _body(request: Request) -> bytes:
    limit = get_settings().inbound_mail_body_limit_bytes
    length = request.headers.get("content-length")
    if length is not None and length.isdigit() and int(length) > limit:
        raise HTTPException(status.HTTP_413_CONTENT_TOO_LARGE, "The message is too large.")
    body = bytearray()
    async for chunk in request.stream():
        body.extend(chunk)
        if len(body) > limit:
            raise HTTPException(status.HTTP_413_CONTENT_TOO_LARGE, "The message is too large.")
    return bytes(body)


@router.post("/inbound-mail", status_code=status.HTTP_202_ACCEPTED)
async def inbound_mail(request: Request) -> dict:
    if not get_settings().inbound_mail_domain:
        raise HTTPException(status.HTTP_404_NOT_FOUND)
    if not _authorised(request.headers.get("authorization")):
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Bad token.")
    raw = await _body(request)
    try:
        given = MailIn.model_validate_json(raw)
        attachments = [
            push_sources.Attachment(
                a.filename, a.content_type, push_sources.decode_attachment(a.content)
            )
            for a in given.attachments
        ]
    except (ValidationError, ValueError) as exc:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_CONTENT, "The body isn't a message."
        ) from exc
    mail = push_sources.Mail(
        recipients=given.to,
        sender=given.sender,
        subject=given.subject,
        message_id=given.message_id,
        in_reply_to=given.in_reply_to,
        references=given.references,
        text=given.text,
        spf=given.spf,
        dkim=given.dkim,
        attachments=attachments,
    )
    reached = await run_in_threadpool(push_sources.receive_mail, mail)
    return {"reached": reached}
