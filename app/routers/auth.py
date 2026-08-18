from fastapi import APIRouter, Depends, HTTPException
from fastapi.security import OAuth2PasswordBearer
from jwt import PyJWTError
from sqlalchemy.orm import Session

from .. import models, schemas, security
from ..database import get_db

router = APIRouter(prefix="/api/auth", tags=["auth"])

# tokenUrl is only used to populate Swagger's "Authorize" UI; the actual
# login endpoint below accepts a plain JSON body, not a form.
oauth2_scheme = OAuth2PasswordBearer(tokenUrl="/api/auth/login", auto_error=False)


def get_current_user(
    token: str = Depends(oauth2_scheme), db: Session = Depends(get_db)
) -> models.User:
    if not token:
        raise HTTPException(status_code=401, detail="Not authenticated")
    try:
        payload = security.decode_access_token(token)
        user_id = int(payload["sub"])
    except (PyJWTError, KeyError, ValueError):
        raise HTTPException(status_code=401, detail="Invalid or expired token")

    user = db.query(models.User).filter(models.User.id == user_id).first()
    if not user:
        raise HTTPException(status_code=401, detail="User not found")
    return user


@router.post("/signup", response_model=schemas.TokenOut)
def signup(payload: schemas.SignupIn, db: Session = Depends(get_db)):
    username = payload.username.strip().lower()
    existing = db.query(models.User).filter(models.User.username == username).first()
    if existing:
        raise HTTPException(status_code=400, detail="That username is already taken.")

    user = models.User(username=username, password_hash=security.hash_password(payload.password))
    db.add(user)
    db.commit()
    db.refresh(user)

    token = security.create_access_token(user.id, user.username)
    return schemas.TokenOut(access_token=token, username=user.username)


@router.post("/login", response_model=schemas.TokenOut)
def login(payload: schemas.LoginIn, db: Session = Depends(get_db)):
    username = payload.username.strip().lower()
    user = db.query(models.User).filter(models.User.username == username).first()
    if not user or not security.verify_password(payload.password, user.password_hash):
        raise HTTPException(status_code=401, detail="Incorrect username or password.")

    token = security.create_access_token(user.id, user.username)
    return schemas.TokenOut(access_token=token, username=user.username)


@router.get("/me", response_model=schemas.UserOut)
def me(current_user: models.User = Depends(get_current_user)):
    return current_user
