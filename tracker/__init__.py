# SPDX-License-Identifier: Apache-2.0
"""pytracking forwarding core.

Only the forwarder (run by cron) talks to providers; the receiver never makes
outbound calls (plan.md lines 3-4). Provider adapters live in
``tracker.adapters`` so private add-ons can be loaded through the same entry
point without the core importing them (plan.md line 9).
"""
