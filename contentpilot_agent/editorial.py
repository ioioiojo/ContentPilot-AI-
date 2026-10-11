"""Assigned reviewers get access only to explicitly delegated jobs."""

import time

from fastapi import Depends, HTTPException
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select, update

from .database import Job, ReviewAssignment, ReviewAudit, ReviewerRole, User


class ReviewerInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    enabled: bool


class AssignmentInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    revision: int = Field(ge=0)
    reviewer_id: str | None = None


def reviewer_access(s, job, user):
    assignment = s.get(ReviewAssignment, job.id)
    if job.owner_id == user.id:
        return True
    role = s.get(ReviewerRole, user.id)
    return bool(assignment and assignment.reviewer_id == user.id and role and role.enabled)


def register_editorial(app, db, current_user, admin_user, owned, job_data):
    @app.get("/api/reviewers")
    def reviewers(user=Depends(current_user)):
        with db.session() as s:
            return [
                {"id": u.id, "username": u.username}
                for u in s.scalars(
                    select(User)
                    .join(ReviewerRole, ReviewerRole.user_id == User.id)
                    .where(ReviewerRole.enabled.is_(True), User.active.is_(True))
                ).all()
            ]

    @app.put("/api/admin/reviewers/{user_id}")
    def set_reviewer(user_id: str, data: ReviewerInput, user=Depends(admin_user)):
        with db.session() as s:
            if not s.get(User, user_id):
                raise HTTPException(404, "用户不存在")
            row = s.get(ReviewerRole, user_id)
            if row:
                row.enabled = data.enabled
            else:
                s.add(ReviewerRole(user_id=user_id, enabled=data.enabled))
        return {"ok": True}

    @app.get("/api/jobs/{job_id}/assignment")
    def assignment_detail(job_id: str, user=Depends(current_user)):
        with db.session() as s:
            job = s.get(Job, job_id)
            if not job or not reviewer_access(s, job, user):
                raise HTTPException(404, "任务不存在")
            row = s.get(ReviewAssignment, job_id)
            return {
                "reviewer_id": row.reviewer_id if row else None,
                "reviewer_name": s.get(User, row.reviewer_id).username if row else None,
                "is_owner": job.owner_id == user.id,
            }

    @app.put("/api/jobs/{job_id}/assignment")
    def assign(job_id: str, data: AssignmentInput, user=Depends(current_user)):
        with db.session() as s:
            job = owned(s, Job, job_id, user)
            if job.status not in {"pending_review", "rejected"}:
                raise HTTPException(409, "仅待审核或驳回任务可分配审核")
            if data.reviewer_id:
                reviewer = s.get(User, data.reviewer_id)
                role = s.get(ReviewerRole, data.reviewer_id)
                if (
                    data.reviewer_id == user.id
                    or not reviewer
                    or not reviewer.active
                    or not role
                    or not role.enabled
                ):
                    raise HTTPException(422, "请选择启用的独立审稿人")
            if not s.execute(
                update(Job)
                .where(
                    Job.id == job_id,
                    Job.revision == data.revision,
                    Job.status.in_(["pending_review", "rejected"]),
                )
                .values(revision=Job.revision + 1, updated_at=time.time())
            ).rowcount:
                raise HTTPException(409, "任务版本已变化，请刷新")
            row = s.get(ReviewAssignment, job_id)
            if data.reviewer_id:
                if row:
                    row.reviewer_id, row.assigned_at = data.reviewer_id, time.time()
                else:
                    s.add(ReviewAssignment(job_id=job_id, reviewer_id=data.reviewer_id))
            elif row:
                s.delete(row)
            s.add(
                ReviewAudit(
                    job_id=job_id,
                    user_id=user.id,
                    action="assign",
                    note="分配审稿人：" + (data.reviewer_id or "撤销分配"),
                    contents=job.contents or [],
                    revision=data.revision + 1,
                )
            )
        return {"ok": True}

    @app.get("/api/review-inbox")
    def inbox(user=Depends(current_user)):
        with db.session() as s:
            role = s.get(ReviewerRole, user.id)
            if not role or not role.enabled:
                return []
            return [
                job_data(job, False)
                for job in s.scalars(
                    select(Job)
                    .join(ReviewAssignment, ReviewAssignment.job_id == Job.id)
                    .where(
                        ReviewAssignment.reviewer_id == user.id,
                        Job.status.in_(["pending_review", "rejected"]),
                    )
                    .order_by(Job.updated_at.desc())
                    .limit(100)
                )
            ]
