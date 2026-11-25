---
id: sop-batch-safety-check
domain: sop
risk_tier: high
version: 1.0
tenant: all
---
# SOP-BS-09 Batch Safety Verification Protocol
Summary: To verify batch safety, confirm the lot status is not HOLD or QUARANTINE.

Facts:
- To verify batch safety, confirm the lot status is not HOLD or QUARANTINE.
- Review the Certificate of Analysis and confirm expiry is more than 30 days away.
- The triage status is GREEN when all batch safety checks pass.
- The triage status is AMBER when one minor flag exists and an operator must review within 8 hours.
- The triage status is RED when the lot is on hold and QA must be notified immediately.

Related: [[sop-quarantine]], [[sop-lot-release]]
