import logging

from fastapi import APIRouter, Depends, File, Form, HTTPException, Response, UploadFile
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from ledger.chat.service import answer
from ledger.config import get_settings
from ledger.db.engine import get_session
from ledger.imports.parsing import detect_kind
from ledger.imports.service import ImportError_, create_batch, store_attachment
from ledger.jobs.worker import notify_worker
from ledger.models import Attachment, ChatMessage, ChatSession
from ledger.schemas import ChatMessageOut, ChatSessionIn, ChatSessionOut
from ledger.statements.service import create_statement

log = logging.getLogger(__name__)
router = APIRouter(prefix="/chat", tags=["chat"])


async def _chat(session: AsyncSession, chat_id: int) -> ChatSession:
    chat = await session.get(ChatSession, chat_id)
    if chat is None:
        raise HTTPException(404, "Conversation not found")
    return chat


async def _messages(session: AsyncSession, where) -> list[ChatMessageOut]:
    rows = (
        await session.execute(
            select(ChatMessage, Attachment.filename)
            .outerjoin(Attachment, Attachment.id == ChatMessage.attachment_id)
            .where(where)
            .order_by(ChatMessage.id)
        )
    ).all()
    return [
        ChatMessageOut(
            id=m.id,
            session_id=m.session_id,
            role=m.role,
            content=m.content,
            attachment_id=m.attachment_id,
            attachment_name=name,
            attachment_kind=detect_kind(name) if name else None,
            queries=m.queries or [],
            created_at=m.created_at,
        )
        for m, name in rows
    ]


@router.get("/sessions", response_model=list[ChatSessionOut])
async def list_sessions(session: AsyncSession = Depends(get_session)):
    return (await session.scalars(select(ChatSession).order_by(ChatSession.updated_at.desc()).limit(100))).all()


@router.post("/sessions", response_model=ChatSessionOut, status_code=201)
async def create_session(body: ChatSessionIn, session: AsyncSession = Depends(get_session)):
    chat = ChatSession(title=body.title or "New conversation")
    session.add(chat)
    await session.commit()
    await session.refresh(chat)
    return chat


@router.delete("/sessions/{chat_id}", status_code=204)
async def delete_session(chat_id: int, session: AsyncSession = Depends(get_session)):
    await session.delete(await _chat(session, chat_id))
    await session.commit()
    return Response(status_code=204)


@router.get("/sessions/{chat_id}/messages", response_model=list[ChatMessageOut])
async def list_messages(chat_id: int, session: AsyncSession = Depends(get_session)):
    await _chat(session, chat_id)
    return await _messages(session, ChatMessage.session_id == chat_id)


@router.post("/sessions/{chat_id}/messages", response_model=list[ChatMessageOut])
async def send_message(
    chat_id: int,
    content: str = Form(""),
    file: UploadFile | None = File(None),
    session: AsyncSession = Depends(get_session),
):
    """Store the user's message (and optional document), answer it, return the new messages."""
    chat = await _chat(session, chat_id)
    content = content.strip()[:8000]
    attachment_id = None
    if file is not None and file.filename:
        if detect_kind(file.filename) is None:
            raise HTTPException(422, "Attach a PDF, image, CSV or Excel file")
        limit = get_settings().max_upload_mb * 1024 * 1024
        data = await file.read(limit + 1)
        if len(data) > limit:
            raise HTTPException(422, f"File is larger than {get_settings().max_upload_mb} MB")
        attachment_id = (await store_attachment(session, data, file.filename, file.content_type, source="chat")).id
    if not content and attachment_id is None:
        raise HTTPException(422, "Type a question or attach a document")

    user_msg = ChatMessage(session_id=chat.id, role="user", content=content, attachment_id=attachment_id, queries=[])
    session.add(user_msg)
    chat.updated_at = func.now()
    if chat.title == "New conversation":
        chat.title = (content or (file.filename if file else "") or "New conversation")[:80]
    await session.commit()
    first_id = user_msg.id

    try:
        await answer(session, chat, user_msg)
        await session.commit()
    except Exception as exc:
        log.exception("chat answer failed")
        await session.rollback()
        session.add(ChatMessage(session_id=chat_id, role="error", content=f"The assistant failed: {exc}"[:2000], queries=[]))
        await session.commit()
    return await _messages(session, (ChatMessage.session_id == chat_id) & (ChatMessage.id >= first_id))


async def _attachment_bytes(session: AsyncSession, attachment_id: int) -> tuple[bytes, str, str]:
    row = (
        await session.execute(
            select(Attachment.content, Attachment.filename, Attachment.mime_type).where(Attachment.id == attachment_id)
        )
    ).first()
    if row is None:
        raise HTTPException(404, "Attachment not found")
    return row.content, row.filename, row.mime_type


@router.post("/attachments/{attachment_id}/statement")
async def file_as_statement(attachment_id: int, session: AsyncSession = Depends(get_session)):
    data, name, mime = await _attachment_bytes(session, attachment_id)
    try:
        st = await create_statement(session, data, name, mime)
    except ImportError_ as exc:
        raise HTTPException(422, str(exc)) from None
    await session.commit()
    notify_worker()
    return {"statement_id": st.id}


@router.post("/attachments/{attachment_id}/import")
async def import_attachment(attachment_id: int, session: AsyncSession = Depends(get_session)):
    data, name, mime = await _attachment_bytes(session, attachment_id)
    try:
        batch = await create_batch(session, data, name, mime)
    except ImportError_ as exc:
        raise HTTPException(422, str(exc)) from None
    await session.commit()
    notify_worker()
    return {"batch_id": batch.id}
