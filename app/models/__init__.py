"""
Import all models here so SQLAlchemy's metadata is populated
when Alembic runs autogenerate. Order matters for FK resolution.
"""
from app.models.content import (  # noqa: F401
    ApprovalAction, ApprovalActionType, BrandProfile,
    ContentStatus, ContentType, ContentVersion, DraftContent,
    GenerationJob, JobStatus, ToneType, Workspace,
    WorkspaceMember, WorkspaceRole,
)
from app.models.scheduler import (  # noqa: F401
    DayOfWeek, EvergreenCandidate, InstagramAccount,
    OptimalTimeSlot, PostMetrics, PostPublishLog,
    PublishErrorCode, PublishStatus, ScheduledPost,
)
from app.models.auth import (  # noqa: F401
    AuthProvider, BillingInterval, CreditActionType,
    CreditBalance, CreditTransaction, Plan, PlanTier,
    Subscription, SubscriptionStatus, User, UserSession,
    CREDIT_COSTS, PLAN_MONTHLY_CREDITS,
)
