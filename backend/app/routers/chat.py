"""
Chat endpoint — powers the sidebar chat in the frontend.

Reasoning logic lives in app.services.chat_service.ChatService; this router
only handles the HTTP contract.
"""

from fastapi import APIRouter, BackgroundTasks, Depends

from app.models.schemas import ChatMessage, ChatRequest, ChatResponse, MessageRole
from app.routers.auth import get_current_user
from app.services.chat_service import ChatService
from app.services.event_bus import EventBus, get_event_bus
from app.services.store import StoreProtocol, get_store

router = APIRouter(prefix="/chat", tags=["chat"], dependencies=[Depends(get_current_user)])


def get_chat_service(
    background_tasks: BackgroundTasks,
    store: StoreProtocol = Depends(get_store),
    event_bus: EventBus = Depends(get_event_bus),
) -> ChatService:
    return ChatService(store, event_bus, background_tasks)


@router.get("/history", response_model=list[ChatMessage])
def get_history(store: StoreProtocol = Depends(get_store)):
    return store.get_chat_history()


@router.post("/message", response_model=ChatResponse)
async def send_message(
    request: ChatRequest,
    store: StoreProtocol = Depends(get_store),
    chat_service: ChatService = Depends(get_chat_service),
):
    store.add_chat_message(MessageRole.user, request.message)
    reply = await chat_service.reply_to(request.message)
    store.add_chat_message(MessageRole.agent, reply)
    return ChatResponse(reply=reply)
