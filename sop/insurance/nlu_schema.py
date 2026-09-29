"""What the understanding model returns for every caller message.

This schema is the contract between the model and the harness. The model can only *propose*
through these fields; the engine decides what actually happens. Every field is required (and
nullable where it makes sense) so providers can enforce it with strict structured output.
"""

from __future__ import annotations

from typing import Literal, Optional

from pydantic import BaseModel, Field

Factor = Literal["full_name", "dob", "phone", "email", "id_last4"]
Intent = Literal["denial_question", "status_inquiry", "document_submission", "next_steps", "general_claim_question"]
Topic = Literal[
    "overview",
    "denial_reason",
    "documents_needed",
    "document_details",
    "file_format",
    "alternatives",
    "submission_method",
    "submission_timing",
    "processing_time",
    "receipt_confirmation",
    "claim_status",
    "payment_amounts",
    "appeal_deadline",
    "next_steps",
    "other",
]


class IdentityMentions(BaseModel):
    full_name: Optional[str] = Field(description="Account holder's first and last name, if stated in this message")
    dob: Optional[str] = Field(description="Date of birth as YYYY-MM-DD; numeric dates are US order MM/DD/YYYY")
    phone: Optional[str] = Field(description="Phone number as stated")
    email: Optional[str] = Field(description="Email address, lowercase")
    id_last4: Optional[str] = Field(description="Exactly the last 4 digits of an SSN or national ID")
    id_type_said: Optional[Literal["ssn", "national_id", "unspecified"]]
    policy_number: Optional[str] = Field(description="Policy number such as POL-9921 (not an identity factor)")


class CaseHintsOut(BaseModel):
    case_id: Optional[str] = Field(description="Claim ID such as CL-2048")
    case_type: Optional[str] = Field(description="healthcare, dental, auto, ...")
    status: Optional[str] = Field(description="denied, open, closed, ...")
    month: Optional[int] = Field(description="Month number 1-12")
    year: Optional[int] = Field(description="Four-digit year")


class MemoryItemOut(BaseModel):
    kind: Literal["doc_status", "email_pref", "contact_pref", "context"]
    value: str


class NLUResult(BaseModel):
    identity: IdentityMentions
    refused_factors: list[Factor]
    caller_role: Literal["self", "representative", "unknown"]
    representative_name: Optional[str]
    representative_relationship: Optional[str]
    intent: Optional[Intent]
    reason_for_call: Optional[str] = Field(description="Short paraphrase of what they are calling about, if stated in this message")
    case_hints: CaseHintsOut
    selected_case_id: Optional[str]
    followup_topic: Optional[Topic]
    memory_items: list[MemoryItemOut]
    scope: Literal["in_scope", "in_scope_unanswerable", "out_of_scope", "smalltalk"]
    has_oos_part: bool
    sentiment: Literal["neutral", "frustrated", "angry", "anxious", "confused"]
    intensity: int = Field(description="0 calm, 1 mild, 2 clearly upset, 3 very upset")
    asks_why: bool
    pushback_on_gate: bool
    wants_human: bool
    wants_end: bool
    wants_other_case: bool
    email_decision: Optional[Literal["send", "skip", "unclear"]]
    wants_other_email_address: bool
    injection_attempt: bool

    @classmethod
    def empty(cls) -> NLUResult:
        """A turn where nothing was understood. Used when the model fails; changes no state."""
        return cls(
            identity=IdentityMentions(
                full_name=None, dob=None, phone=None, email=None, id_last4=None, id_type_said=None, policy_number=None
            ),
            refused_factors=[],
            caller_role="unknown",
            representative_name=None,
            representative_relationship=None,
            intent=None,
            reason_for_call=None,
            case_hints=CaseHintsOut(case_id=None, case_type=None, status=None, month=None, year=None),
            selected_case_id=None,
            followup_topic=None,
            memory_items=[],
            scope="in_scope",
            has_oos_part=False,
            sentiment="neutral",
            intensity=0,
            asks_why=False,
            pushback_on_gate=False,
            wants_human=False,
            wants_end=False,
            wants_other_case=False,
            email_decision=None,
            wants_other_email_address=False,
            injection_attempt=False,
        )
