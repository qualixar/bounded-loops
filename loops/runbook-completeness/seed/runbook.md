# API Gateway 5xx Spike Runbook

## Summary

This runbook covers response to a sustained spike in 5xx errors from the
public API gateway.

## Severity

SEV-2 if 5xx rate exceeds 5% of traffic for more than 5 minutes.

## Detection

Alert `apigw-5xx-rate-high` fires when the `apigw_5xx_ratio` metric exceeds
0.05 over a 5-minute window. Confirm in the gateway dashboard.

## Diagnosis

Check the gateway's upstream error breakdown by backend service. Correlate
with recent deploys in the last 30 minutes. Check upstream service health
endpoints directly to rule out a gateway-only issue.

## Mitigation

If a recent deploy correlates with the spike, roll traffic back to the
previous backend version using the deploy tool's `promote --previous`
command. If no deploy correlates, fail over to the standby region via the
gateway's traffic-manager console.
