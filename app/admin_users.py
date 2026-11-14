from datetime import datetime
from typing import Callable, Literal

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import asc, delete, desc, func, select
from sqlalchemy.orm import Session

from .database import get_db
from .models import (
    AdminUser,
    Attempt,
    AuthEmailToken,
    ExamAttempt,
    ExamQuestionRevision,
    ExamRun,
    ExamWrongQuestion,
    PracticeAttempt,
    User,
    UserSession,
    WrongQuestion,
)
from .schemas import (
    AdminUserCreate,
    AdminUserListOut,
    AdminUserOut,
    AdminUserUpdate,
)
from .security import hash_password
from .stats import build_overview, build_trend

# 用户管理只面向 C 端（学员）用户：users 表只剩学员（D12 拆表），管理员在 admin_users 独立表。
JLPT_LEVELS = {"N5", "N4", "N3", "N2", "N1"}

MIN_PASSWORD_LENGTH = 8

SORT_COLUMNS = {
    "id": User.id,
    "username": User.username,
    "display_name": User.display_name,
    "current_level": User.current_level,
    "created_at": User.created_at,
    "last_active_at": User.last_active_at,
}


def create_admin_users_router(admin_dependency: Callable) -> APIRouter:
    router = APIRouter(prefix="/api/admin/users", tags=["admin-users"])

    def _get_user_or_404(db: Session, user_id: str) -> User:
        user = db.get(User, user_id)
        if user is None:
            raise HTTPException(status_code=404, detail="User not found")
        return user

    def _validate_password(password: str) -> str:
        if len(password) < MIN_PASSWORD_LENGTH:
            raise HTTPException(
                status_code=400,
                detail=f"Password must be at least {MIN_PASSWORD_LENGTH} characters",
            )
        return password

    def _validate_level(level: str) -> str:
        normalized = level.strip().upper()
        if normalized not in JLPT_LEVELS:
            raise HTTPException(status_code=400, detail="Invalid JLPT level")
        return normalized

    @router.get("", response_model=AdminUserListOut)
    def admin_users(
        limit: int = Query(100, ge=1, le=500),
        offset: int = Query(0, ge=0),
        keyword: str | None = None,
        sortField: str | None = None,
        sortOrder: str | None = None,
        db: Session = Depends(get_db),
        admin: AdminUser = Depends(admin_dependency),
    ) -> AdminUserListOut:
        _ = admin
        stmt = select(User)
        count_stmt = select(func.count(User.id))

        if keyword:
            pattern = f"%{keyword.strip()}%"
            condition = User.username.ilike(pattern) | User.display_name.ilike(pattern) | User.email.ilike(pattern)
            stmt = stmt.where(condition)
            count_stmt = count_stmt.where(condition)

        column = SORT_COLUMNS.get(sortField or "", User.id)
        direction = desc if sortOrder == "descend" else asc
        stmt = stmt.order_by(direction(column)).limit(limit).offset(offset)

        users = db.scalars(stmt).all()
        total = db.scalar(count_stmt) or 0
        return AdminUserListOut(
            list=[AdminUserOut.model_validate(item) for item in users],
            total=total,
        )

    @router.post("", response_model=AdminUserOut)
    def admin_create_user(
        payload: AdminUserCreate,
        db: Session = Depends(get_db),
        admin: AdminUser = Depends(admin_dependency),
    ) -> User:
        _ = admin
        username = payload.username.strip()
        display_name = payload.display_name.strip()
        password = payload.password.strip()
        if not username or not display_name or not password:
            raise HTTPException(
                status_code=400,
                detail="Username, display name, and password are required",
            )
        existing = db.scalar(select(User).where(User.username == username))
        if existing:
            raise HTTPException(status_code=409, detail="Username already exists")

        user = User(
            username=username,
            display_name=display_name,
            password_hash=hash_password(_validate_password(password)),
            current_level=_validate_level(payload.current_level),
        )
        db.add(user)
        db.commit()
        db.refresh(user)
        return user

    @router.put("/{user_id}", response_model=AdminUserOut)
    def admin_update_user(
        user_id: str,
        payload: AdminUserUpdate,
        db: Session = Depends(get_db),
        admin: AdminUser = Depends(admin_dependency),
    ) -> User:
        _ = admin
        user = _get_user_or_404(db, user_id)

        if payload.display_name is not None:
            display_name = payload.display_name.strip()
            if not display_name:
                raise HTTPException(status_code=400, detail="Display name cannot be empty")
            user.display_name = display_name

        if payload.current_level is not None:
            user.current_level = _validate_level(payload.current_level)

        if payload.password is not None:
            password = payload.password.strip()
            if not password:
                raise HTTPException(status_code=400, detail="Password cannot be empty")
            user.password_hash = hash_password(_validate_password(password))
            # 重置密码后，该用户所有已登录设备立即失效
            for session in db.scalars(select(UserSession).where(UserSession.user_id == user.id)):
                db.delete(session)

        db.add(user)
        db.commit()
        db.refresh(user)
        return user

    @router.post("/{user_id}/disable", response_model=AdminUserOut)
    def admin_disable_user(
        user_id: str,
        db: Session = Depends(get_db),
        admin: AdminUser = Depends(admin_dependency),
    ) -> User:
        """停用（T-031）：保留数据 + 禁止登录 + 立即失效现有会话；可恢复，与删除互补。"""
        _ = admin
        user = _get_user_or_404(db, user_id)
        if user.disabled_at is None:
            user.disabled_at = datetime.utcnow()
            # 会话即时失效依赖此处删行；get_app_user 不做逐请求检查。
            for session in db.scalars(select(UserSession).where(UserSession.user_id == user.id)):
                db.delete(session)
            db.add(user)
            db.commit()
            db.refresh(user)
        return user

    @router.post("/{user_id}/enable", response_model=AdminUserOut)
    def admin_enable_user(
        user_id: str,
        db: Session = Depends(get_db),
        admin: AdminUser = Depends(admin_dependency),
    ) -> User:
        """恢复：仅清 disabled_at；会话已在停用时删除，重新登录即恢复。"""
        _ = admin
        user = _get_user_or_404(db, user_id)
        if user.disabled_at is not None:
            user.disabled_at = None
            db.add(user)
            db.commit()
            db.refresh(user)
        return user

    @router.delete("/{user_id}")
    def admin_delete_user(
        user_id: str,
        db: Session = Depends(get_db),
        admin: AdminUser = Depends(admin_dependency),
    ) -> dict:
        _ = admin
        user = _get_user_or_404(db, user_id)

        # 答题/校对记录与其他表有外键引用，存在记录的用户不允许删除，
        # 避免连带清空学习数据。
        has_activity = any(
            db.scalar(select(func.count()).where(table.user_id == user_id))
            for table in (
                Attempt,
                PracticeAttempt,
                ExamAttempt,
                ExamRun,
                WrongQuestion,
                ExamWrongQuestion,
                ExamQuestionRevision,
            )
        )
        if has_activity:
            raise HTTPException(
                status_code=409,
                detail="User has learning or review records and cannot be deleted",
            )

        # 用 DML 而非逐行 db.delete：User/UserSession 之间无 relationship()，
        # SQLAlchemy unit of work 不识别依赖顺序，会把 DELETE users 排在
        # DELETE user_sessions 之前（SQLite 不强制 FK 掩盖了该问题，Postgres 报
        # user_sessions_user_id_fkey 违规）。DML 立即执行，顺序确定。
        # auth_email_tokens：T-028 注册/验证/重置令牌，不清理会让刚注册用户无法删除。
        db.execute(delete(UserSession).where(UserSession.user_id == user_id))
        db.execute(delete(AuthEmailToken).where(AuthEmailToken.user_id == user_id))
        db.delete(user)
        db.commit()
        return {"deleted": user_id}

    @router.get("/{user_id}", response_model=AdminUserOut)
    def admin_user_detail(
        user_id: str,
        db: Session = Depends(get_db),
        admin: AdminUser = Depends(admin_dependency),
    ) -> User:
        _ = admin
        return _get_user_or_404(db, user_id)

    @router.get("/{user_id}/stats/overview")
    def admin_user_stats_overview(
        user_id: str,
        level: str | None = None,
        db: Session = Depends(get_db),
        admin: AdminUser = Depends(admin_dependency),
    ) -> dict:
        _ = admin
        user = _get_user_or_404(db, user_id)
        # admin 默认看全部级别；?level=N3 可只看某一级。
        return build_overview(db, user, level, all_levels=level is None)

    @router.get("/{user_id}/stats/trend")
    def admin_user_stats_trend(
        user_id: str,
        metric: Literal["accuracy", "volume"] = "accuracy",
        granularity: Literal["day", "week", "month"] = "day",
        days: int = Query(90, ge=1, le=3660),
        level: str | None = None,
        db: Session = Depends(get_db),
        admin: AdminUser = Depends(admin_dependency),
    ) -> dict:
        _ = admin
        _ = metric  # 与学生端一致：两种指标共用同一份 points，前端负责呈现
        user = _get_user_or_404(db, user_id)
        return build_trend(
            db,
            user,
            granularity=granularity,
            days=days,
            level=level,
            all_levels=level is None,
        )

    return router
