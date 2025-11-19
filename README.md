# Retention Bot

## Overview
Retention Bot is a SMS assistant for small businesses to retain and support existing customers. Every message is tied to an existing customer record and service history. There is no promotional or affiliate traffic—only service-related follow-ups, scheduling help, or status checks for people who already shared their mobile number with the company.

## Who Receives Messages
- Current service members who confirm their mobile number during onboarding or recent visits.
- Recently cancelled or paused customers who agreed to continued check-ins.
- No purchased leads, contests, or age-gated segments. 

## Customer SMS Journey
1. Customer receives a proactive follow-up or replies to an existing thread.
2. Incoming SMS hits the `/sms` webhook, is verified against the Twilio signature, and logged with the user’s state machine entry.
3. GPT + the IntentionFlow FSM craft a reply that references their last service, open issues, and allowed transitions. Typical message: “Hey Jamie! Still seeing any pest issues we can help with?”.
4. Conversations continue until the customer confirms resolution, schedules a follow-up, or says “stop”.

## Staff Console Experience
- **Conversation List & Detail:** Live roster of contacts with search, sort, and per-thread detail plus export for audit requests.
- **Status Controls:** Supervisors can edit the FSM state (start, interested, action_sqft, follow_up, pause, stop, done) when handing off to human techs.
- **Reach Out Settings:** Preview candidate lists (e.g., “days since cancelled”) and launch a throttled batch to avoid high-frequency bursts.
- **Authentication:** Single admin login (Flask-Login) with hashed password.

## Proactive Reach Out
- Candidate source comes from internal DB rows (service status, phone, days since service).
- Runs respect an adjustable `max_active` cap and skip contacts already in `stop` or `done`.
- Each send logs the run, message body, and Twilio SID for auditing.

## SMS Content & Cadence
- Tone is conversational, service-focused, and short (under 320 characters).
- Triggered replies are one-for-one: a customer text produces a single contextual response.

## Compliance & Safeguards
- Webhook signatures verified with Twilio’s `RequestValidator`.
- STOP, STOPALL, UNSUBSCRIBE, CANCEL, END, and QUIT immediately flag the thread as `stop` and return “Messages Stopped”. Future replies are suppressed until a human confirms renewed consent.
- Manual STOP actions from the console also fire the confirmation text so logs stay synchronized.
- Outbound traffic only leaves the system when `OUTBOUND_LIVE_TOGGLE=1`, preventing accidental sends in staging.
- Staff can export full transcripts to answer carrier or customer complaints.
- CSRF protection and login requirements cover every console action.
