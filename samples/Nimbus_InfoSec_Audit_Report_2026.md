SYNTHETIC SAMPLE DOCUMENT — FOR DEMONSTRATION ONLY

# Information Security Audit Report
Supplier: Nimbus Data Services Pvt Ltd
Audit type: Remote and on-site audit (Bengaluru delivery centre)
Audit period: 06 October 2026 to 17 October 2026
Report date: 24 October 2026
Lead auditor: External Assurance Partner (synthetic)
Contact: audit.lead@example.com

## 1. Scope
The audit covered information security, data privacy and sub-processor management for the client data
processing services delivered by Nimbus Data Services. Nimbus processes client personal data (PII), including
customer names, phone numbers and account identifiers. 34 Nimbus staff hold access to client systems.

## 2. Overall opinion
Needs improvement. Two findings require urgent remediation before the next quarterly review. Controls for
encryption of backups and approval of sub-processors are not operating effectively.

## 3. Certifications
The ISO 27001 certificate presented (certificate no. SYN-27K-4471) expired on 30 September 2026. A surveillance
audit is scheduled but not yet completed. No SOC 2 report is available.

## 4. Findings

Finding F-01: Multi-factor authentication not enforced for VPN administrator accounts
Severity: High
Control area: Access control
Status: Open
Observation: 6 of 9 administrator accounts can connect to the VPN with a password only. MFA rollout is planned
for Q1 2027.
Recommendation: Enforce MFA for all privileged and remote access within 30 days.

Finding F-02: Unencrypted backups of client PII stored on offshore NAS
Severity: Critical
Control area: Data protection
Status: Open
Observation: Nightly backups containing client PII are written unencrypted to a NAS device at the Manila
facility. Physical access to the server room is shared with another tenant.
Recommendation: Encrypt backups at rest with managed keys and restrict physical access immediately.

Finding F-03: Quarterly user access reviews not performed
Severity: Medium
Control area: Access control
Status: Open
Observation: No evidence of access reviews for Q2 and Q3 2026. Four leavers still had active accounts.
Recommendation: Run access reviews quarterly and remove leaver accounts within 24 hours.

Finding F-04: Security awareness training records incomplete
Severity: Low
Control area: HR security
Status: Closed
Observation: Training records were missing for 3 new joiners; completed during fieldwork.
Recommendation: Track training completion in the HR system.

Finding F-05: Sub-processor engaged without client approval
Severity: High
Control area: Sub-processor management
Status: Open
Observation: Overflow call-centre work is subcontracted to a third party in the Philippines that receives
client PII. The sub-processor is not listed in the data processing agreement and was not approved.
Recommendation: Suspend data sharing until the sub-processor is approved and contractually bound.

Finding F-06: Incident response plan not tested in 2026
Severity: Medium
Control area: Incident response
Status: Open
Observation: The incident response plan exists but no tabletop exercise has been run since March 2025.
Recommendation: Run a tabletop exercise within 60 days and record lessons learned.

## 5. Positive observations
Encryption in transit (TLS 1.2+) is enforced for all client connections. Change management is documented and
followed. Business continuity plan was tested in July 2026.

## 6. Management response
Nimbus management accepted all findings and committed to remediate F-01, F-02 and F-05 within 30 days.
