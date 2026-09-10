from typing import List

from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from .. import models, schemas
from ..database import get_db

# Public leaderboard (Feature 13). Deliberately exposes ONLY username, xp and
# the derived level — never password hashes, emails, ids or badge data.
router = APIRouter(prefix="/api", tags=["leaderboard"])


@router.get("/leaderboard", response_model=List[schemas.LeaderboardEntry])
def leaderboard(db: Session = Depends(get_db)):
    users = (
        db.query(models.User)
        .order_by(models.User.xp.desc(), models.User.id.asc())
        .limit(10)
        .all()
    )
    return [
        schemas.LeaderboardEntry(
            username=u.username,
            xp=u.xp or 0,
            level=(u.xp or 0) // 100,
        )
        for u in users
    ]
