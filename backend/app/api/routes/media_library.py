from __future__ import annotations

from fastapi import APIRouter, Depends, Request
from sqlalchemy.orm import Session

from app.core.deps import CurrentUser, get_current_user, require_permissions
from app.core.permissions import TASK_READ, TASK_WRITE
from app.db.session import get_db
from app.schemas.media_library import (
    EmbyLibraryOut,
    MediaCategoryIn,
    MediaCategoryOut,
    MediaCategorySuggestOut,
    MediaCategoryUpdateIn,
    OpenListStorageOut,
)
from app.services import audit
from app.services import media_library as svc


router = APIRouter()


def _audit(db: Session, request: Request, current: CurrentUser, action: str, target_id: str) -> None:
    audit.write_audit_log(
        db,
        actor_user_id=current.user.id,
        action=action,
        target_type="media_category",
        target_id=target_id,
        ip=request.client.host if request.client else None,
        user_agent=request.headers.get("user-agent"),
        success=True,
    )


@router.get("/openlist/storages", response_model=list[OpenListStorageOut], dependencies=[Depends(require_permissions(TASK_READ))])
def get_openlist_storages(db: Session = Depends(get_db)):
    return svc.list_openlist_storages(db)


@router.get("/emby/libraries", response_model=list[EmbyLibraryOut], dependencies=[Depends(require_permissions(TASK_READ))])
def get_emby_libraries(db: Session = Depends(get_db)):
    return svc.list_emby_libraries(db)


@router.get("/categories", response_model=list[MediaCategoryOut], dependencies=[Depends(require_permissions(TASK_READ))])
def get_categories(db: Session = Depends(get_db)):
    return [svc.category_to_dict(item) for item in svc.list_categories(db)]


@router.post("/categories/suggest", response_model=MediaCategorySuggestOut, dependencies=[Depends(require_permissions(TASK_READ))])
def post_suggest_categories(db: Session = Depends(get_db)):
    return svc.suggest_categories(db)


@router.post("/categories", response_model=MediaCategoryOut, dependencies=[Depends(require_permissions(TASK_WRITE))])
def post_category(
    request: Request,
    payload: MediaCategoryIn,
    current: CurrentUser = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    item = svc.create_category(db, payload.model_dump(exclude_unset=True))
    _audit(db, request, current, "media_category.create", str(item.id))
    db.commit()
    db.refresh(item)
    return svc.category_to_dict(item)


@router.patch("/categories/{category_id}", response_model=MediaCategoryOut, dependencies=[Depends(require_permissions(TASK_WRITE))])
def patch_category(
    request: Request,
    category_id: int,
    payload: MediaCategoryUpdateIn,
    current: CurrentUser = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    item = svc.update_category(db, category_id, payload.model_dump(exclude_unset=True))
    _audit(db, request, current, "media_category.update", str(category_id))
    db.commit()
    db.refresh(item)
    return svc.category_to_dict(item)


@router.delete("/categories/{category_id}", dependencies=[Depends(require_permissions(TASK_WRITE))])
def delete_category(
    request: Request,
    category_id: int,
    current: CurrentUser = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    svc.delete_category(db, category_id)
    _audit(db, request, current, "media_category.delete", str(category_id))
    db.commit()
    return {"ok": True}
