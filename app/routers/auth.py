from fastapi import APIRouter, Depends, HTTPException, Request, Response
from pydantic import BaseModel, Field, field_validator
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.auth import (
    COOKIE, _DUMMY_HASH, check_throttle, clear_failures, end_session, end_user_sessions, hash_password,
    password_problem, record_failure, require_admin, require_user, set_cookie, start_session, verify_password,
)
from app.db import get_session
from app.models import User
from app.schemas import ORM, UtcDatetime

router = APIRouter(tags=["auth"])


def _email(value: str) -> str:
    value = value.strip().lower()
    if "@" not in value or len(value) > 255 or " " in value:
        raise ValueError("enter a valid email address")
    return value


def _password(value: str) -> str:
    problem = password_problem(value)
    if problem:
        raise ValueError(problem)
    return value


class UserRead(ORM):
    id: int
    email: str
    name: str
    is_admin: bool
    active: bool
    last_login_at: UtcDatetime | None


class LoginBody(BaseModel):
    email: str
    password: str


class SetupBody(BaseModel):
    name: str = Field(min_length=1, max_length=100)
    email: str
    password: str

    _e = field_validator("email")(_email)
    _p = field_validator("password")(_password)


class UserCreate(SetupBody):
    is_admin: bool = False


class UserUpdate(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=100)
    is_admin: bool | None = None
    active: bool | None = None
    password: str | None = None

    _p = field_validator("password")(lambda v: v if v is None else _password(v))


class PasswordChange(BaseModel):
    current_password: str
    new_password: str

    _p = field_validator("new_password")(_password)


def _client(request: Request) -> str:
    return f"ip:{request.client.host if request.client else 'unknown'}"


@router.get("/auth/setup-status")
def setup_status(session: Session = Depends(get_session)):
    """Whether the first administrator still has to be created."""
    return {"needs_setup": session.scalar(select(func.count()).select_from(User)) == 0}


@router.post("/auth/setup", response_model=UserRead, status_code=201)
def first_admin(body: SetupBody, request: Request, response: Response, session: Session = Depends(get_session)):
    if session.scalar(select(func.count()).select_from(User)):
        raise HTTPException(409, "setup is already complete; sign in instead")
    user = User(email=body.email, name=body.name.strip(), password_hash=hash_password(body.password), is_admin=True)
    session.add(user)
    session.flush()
    set_cookie(response, start_session(session, user, request.headers.get("user-agent")))
    return user


@router.post("/auth/login", response_model=UserRead)
def login(body: LoginBody, request: Request, response: Response, session: Session = Depends(get_session)):
    email = body.email.strip().lower()
    keys = (f"email:{email}", _client(request))
    check_throttle(*keys)
    user = session.scalar(select(User).where(User.email == email))
    ok = verify_password(body.password, user.password_hash if user else _DUMMY_HASH)
    if not user or not ok or not user.active:
        record_failure(*keys)
        raise HTTPException(401, "incorrect email or password")
    clear_failures(*keys)
    set_cookie(response, start_session(session, user, request.headers.get("user-agent")))
    return user


@router.post("/auth/logout", status_code=204)
def logout(request: Request, response: Response, session: Session = Depends(get_session)):
    end_session(session, request.cookies.get(COOKIE))
    response.delete_cookie(COOKIE, path="/")


@router.get("/auth/me", response_model=UserRead)
def me(user: User = Depends(require_user)):
    return user


@router.post("/auth/password", status_code=204)
def change_password(body: PasswordChange, request: Request, user: User = Depends(require_user),
                    session: Session = Depends(get_session)):
    user = session.merge(user)
    if not verify_password(body.current_password, user.password_hash):
        raise HTTPException(422, "current password is incorrect")
    user.password_hash = hash_password(body.new_password)
    end_user_sessions(session, user.id, keep_token=request.cookies.get(COOKIE))  # sign out other devices
    session.commit()


# ---------------------------------------------------------------- user management (administrators)

@router.get("/users", response_model=list[UserRead], dependencies=[Depends(require_admin)])
def list_users(session: Session = Depends(get_session)):
    return session.scalars(select(User).order_by(User.name)).all()


@router.post("/users", response_model=UserRead, status_code=201, dependencies=[Depends(require_admin)])
def create_user(body: UserCreate, session: Session = Depends(get_session)):
    if session.scalar(select(User).where(User.email == body.email)):
        raise HTTPException(409, f"a user with email {body.email} already exists")
    user = User(email=body.email, name=body.name.strip(), password_hash=hash_password(body.password), is_admin=body.is_admin)
    session.add(user)
    session.commit()
    return user


@router.patch("/users/{user_id}", response_model=UserRead)
def update_user(user_id: int, body: UserUpdate, admin: User = Depends(require_admin), session: Session = Depends(get_session)):
    user = session.get(User, user_id)
    if not user:
        raise HTTPException(404, f"user {user_id} not found")
    changes = body.model_dump(exclude_unset=True)
    if user.id == admin.id and (changes.get("active") is False or changes.get("is_admin") is False):
        raise HTTPException(422, "you cannot deactivate yourself or remove your own administrator rights")
    if "password" in changes:
        password = changes.pop("password")
        if password:
            user.password_hash = hash_password(password)
            end_user_sessions(session, user.id)
    for key, value in changes.items():
        setattr(user, key, value.strip() if isinstance(value, str) else value)
    if changes.get("active") is False:
        end_user_sessions(session, user.id)
    session.commit()
    return user
