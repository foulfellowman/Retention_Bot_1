"""Utility for starting proactive SMS conversations with existing customers."""
from __future__ import annotations

import os
from datetime import datetime, timezone
from typing import Any, Iterable, Mapping, MutableMapping, Optional, Sequence

from db import DB
from gpt import GPTClient
from twilio_test import TwilioSMSClient
from user_context import UserContext
from models import FSMState, ReachOutRun
from sqlalchemy import func, select


class ReachOut:
    """Coordinate proactive outreach flows using existing service clients with throttling."""

    _ENV_THROTTLE_KEYS = ("REACH_OUT_THROTTLE", "REACH_OUT_MAX_ACTIVE")

    def __init__(
        self,
        gpt_client: GPTClient,
        twilio_client: TwilioSMSClient,
        db_factory: type[DB] | None = None,
        max_active_conversations: int | None = None,
    ) -> None:
        self._gpt = gpt_client
        self._twilio = twilio_client
        self._db_factory = db_factory or DB
        self._max_active = max_active_conversations

    def send_bulk(
        self,
        rows: Iterable[Any],
        message_template: Optional[str] = None,
        reset_context: bool = False,
        max_active: int | None = None,
    ) -> dict[str, Any]:
        """Send an initial outbound SMS to each customer row, logging the run.

        Args:
            rows: Iterable containing mappings or objects with a phone number.
            message_template: Optional format string applied per row. When provided,
                the template is formatted with the row (mapping or object attributes).
                Missing keys fall back to the default FSM-driven message.
            reset_context: Clear any prior GPT context for the phone before sending.
            max_active: Override for the concurrent conversation limit; `None` falls back
                to the value passed at construction. Conversations in the `done` state
                are excluded from the count.

        Returns:
            Summary payload containing the run id, per-row results, and aggregate stats.
        """
        results: list[dict[str, Any]] = []
        requested_count = 0
        sent_count = 0
        skipped_count = 0
        throttled_count = 0
        error_count = 0

        limit = self._resolve_max_active(max_active)
        run_db: DB | None = None
        run_log: ReachOutRun | None = None

        try:
            run_db = self._db_factory()
            run_log = ReachOutRun(
                requested=0,
                processed=0,
                sent=0,
                skipped=0,
                throttled=0,
                errors=0,
                context={
                    "max_active_limit": limit,
                    "reset_context": reset_context,
                    "template_provided": bool(message_template),
                },
            )
            run_db.session.add(run_log)
            run_db.session.flush()
            run_id = run_log.run_id

            for row in rows:
                requested_count += 1

                if limit is not None:
                    active_count = self._count_active_conversations(run_db)
                    if active_count >= limit:
                        throttled_count += 1
                        results.append(
                            {
                                "run_id": run_id,
                                "row": row,
                                "status": "skipped",
                                "reason": "throttled",
                                "active_conversations": active_count,
                            }
                        )
                        continue

                phone = self._extract_phone(row)
                if not phone:
                    skipped_count += 1
                    results.append(
                        {
                            "run_id": run_id,
                            "row": row,
                            "status": "skipped",
                            "reason": "missing phone",
                        }
                    )
                    continue

                try:
                    user = self._build_user_context(phone, row)
                except Exception as exc:
                    error_count += 1
                    results.append(
                        {
                            "run_id": run_id,
                            "phone": phone,
                            "status": "error",
                            "error": str(exc),
                        }
                    )
                    continue

                if reset_context:
                    self._gpt.set_context(phone, [])

                body = self._resolve_message(user, row, message_template)

                try:
                    # Ensure FSM state is persisted so throttling counts this conversation
                    user.get_current_state()
                except Exception:
                    pass

                try:
                    self._twilio.send_sms(to_phone=phone, message=body)
                except Exception as exc:
                    error_count += 1
                    results.append(
                        {
                            "run_id": run_id,
                            "phone": phone,
                            "status": "error",
                            "error": str(exc),
                        }
                    )
                    continue

                db_instance = self._db_factory()
                try:
                    self._gpt.insert_with_db_instance(db_instance, body, user)
                finally:
                    try:
                        db_instance.close()
                    except Exception:
                        pass

                sent_count += 1
                results.append(
                    {
                        "run_id": run_id,
                        "phone": phone,
                        "status": "sent",
                        "message": body,
                    }
                )

            processed_count = sent_count + skipped_count + throttled_count + error_count

            summary = {
                "requested": requested_count,
                "processed": processed_count,
                "sent": sent_count,
                "skipped": skipped_count,
                "throttled": throttled_count,
                "errors": error_count,
                "max_active_limit": limit,
            }

            return {
                "run_id": run_id,
                "summary": summary,
                "results": results,
            }
        finally:
            if run_log is not None and run_db is not None:
                try:
                    run_log.requested = requested_count
                    run_log.processed = sent_count + skipped_count + throttled_count + error_count
                    run_log.sent = sent_count
                    run_log.skipped = skipped_count
                    run_log.throttled = throttled_count
                    run_log.errors = error_count
                    run_log.finished_at = datetime.now(timezone.utc)
                    run_db.session.add(run_log)
                    run_db.session.commit()
                except Exception:
                    try:
                        run_db.session.rollback()
                    except Exception:
                        pass
                finally:
                    try:
                        run_db.close()
                    except Exception:
                        pass
            elif run_db is not None:
                try:
                    run_db.close()
                except Exception:
                    pass

    def _build_user_context(self, phone: str, row: Any) -> UserContext:
        """Create or hydrate the UserContext for outreach sending."""
        user = UserContext(phone)

        name = self._value(row, "name")
        first = self._value(row, "first_name")
        last = self._value(row, "last_name")
        if not name:
            parts = [p for p in [first, last] if p]
            name = " ".join(parts)

        services = self._value(row, "services")
        if services is None:
            services = self._value(row, "previous_services")
        services_list = self._coerce_services(services)

        days_since_cancelled = self._value(row, "days_since_cancelled")
        if days_since_cancelled is None:
            days_since_cancelled = self._value(row, "days_since")

        last_service = self._value(row, "last_service") or self._value(row, "primary_service")

        if name or services_list or days_since_cancelled is not None or last_service:
            user.set_user_info(
                name or "",
                services_list,
                self._coerce_int(days_since_cancelled) if days_since_cancelled is not None else 0,
                last_service or "",
            )

        return user

    def _resolve_message(
        self,
        user: UserContext,
        row: Any,
        template: Optional[str],
    ) -> str:
        if template:
            try:
                if isinstance(row, Mapping):
                    return template.format(**row)
                return template.format(**self._to_mapping(row))
            except Exception:
                pass

        snap = user.get_fsm_snapshot()
        return user.reply_for_state(snap)

    @staticmethod
    def _extract_phone(row: Any) -> str | None:
        for key in ("phone_number", "phone", "mobile"):
            value = ReachOut._value(row, key)
            if value:
                text = str(value).strip()
                if text:
                    return text
        return None

    @staticmethod
    def _value(row: Any, key: str) -> Any:
        if isinstance(row, Mapping):
            return row.get(key)
        if hasattr(row, key):
            return getattr(row, key)
        return None

    @staticmethod
    def _to_mapping(row: Any) -> MutableMapping[str, Any]:
        if isinstance(row, Mapping):
            return dict(row)
        attrs = {k: getattr(row, k) for k in dir(row) if not k.startswith("_")}
        return attrs

    @staticmethod
    def _count_active_conversations(state_db: DB) -> int:
        session = state_db.session
        stmt = select(func.count()).select_from(FSMState).where(func.coalesce(FSMState.statename, '') != 'done')
        result = session.execute(stmt).scalar_one()
        return int(result or 0)

    @staticmethod
    def _coerce_services(value: Any) -> list[str]:
        if value is None:
            return []
        if isinstance(value, str):
            return [item.strip() for item in value.split(",") if item.strip()]
        if isinstance(value, Sequence):
            return [str(item).strip() for item in value if str(item).strip()]
        return [str(value).strip()]

    @staticmethod
    def _coerce_int(value: Any) -> int:
        try:
            return int(value)  # type: ignore[arg-type]
        except Exception:
            return 0

    def _resolve_max_active(self, override: int | None) -> int | None:
        if override is not None and override > 0:
            return override

        env_limit = self._load_throttle_from_env()
        if env_limit is not None:
            return env_limit

        return self._max_active if self._max_active and self._max_active > 0 else None

    @classmethod
    def _load_throttle_from_env(cls) -> int | None:
        for env_name in cls._ENV_THROTTLE_KEYS:
            raw_value = os.getenv(env_name)
            if not raw_value:
                continue
            try:
                parsed = int(raw_value)
            except (TypeError, ValueError):
                continue
            if parsed <= 0:
                continue
            return parsed
        return None
