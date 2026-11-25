---
id: pol-access-control
domain: policy
risk_tier: high
version: 1.0
tenant: all
---
# POL-03 Access Control and Four-Eyes Principle
Summary: System roles are reader, operator and approver.

Facts:
- System roles are reader, operator and approver.
- Only an approver may approve a Tier 3 mutation.
- The approver of a mutation must be a different person than the requester.
