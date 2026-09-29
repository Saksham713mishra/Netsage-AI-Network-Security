"""
rule_checker.py
----------------
This is the "Rule checker" deliverable: a plain Python script (no AI)
that scans the pasted show-command output for a case and flags common
config mistakes using regex + basic subnet math. It's meant to run
BEFORE or alongside the AI diagnosis, so we have a deterministic,
explainable second opinion.

Checks implemented:
  1. duplicate_ip        - same IP address shows up on two different interfaces
  2. interface_down       - interface is administratively down or line down
  3. gateway_mismatch      - PC's default gateway IP doesn't match any router
                             interface IP found in the same output
  4. mask_mismatch         - two interfaces claimed to be on the same subnet
                             report different subnet masks
  5. missing_vlan          - a VLAN mentioned in the concept tag / symptom
                             text isn't listed in "show vlan brief" output
  6. missing_route         - a destination network mentioned isn't present
                             in "show ip route" output

This is intentionally simple regex/string matching, not a full CLI
parser - Packet Tracer show output is fairly consistent so this covers
the cases the project brief asks for (duplicate IPs, wrong masks,
gateway mismatch, interface down, missing VLAN, missing routes).
"""

import re
import ipaddress
import json

IP_RE = re.compile(r"\b(\d{1,3}\.\d{1,3}\.\d{1,3}\.\d{1,3})\b")
MASK_RE = re.compile(r"\b(255\.\d{1,3}\.\d{1,3}\.\d{1,3})\b")
VLAN_RE = re.compile(r"\bvlan\s*(\d+)\b", re.IGNORECASE)


def _lines(text):
    return [ln.strip() for ln in text.splitlines() if ln.strip()]


def check_interface_down(text):
    issues = []
    for ln in _lines(text):
        low = ln.lower()
        if "administratively down" in low:
            issues.append({
                "type": "interface_down",
                "severity": "High",
                "message": f"Interface is administratively down (shutdown not removed): \"{ln}\""
            })
        elif re.search(r"\bdown\b.*\bdown\b", low) and ("interface" in low or re.match(r"^(fa|gi|se|vlan|eth)", low)):
            issues.append({
                "type": "interface_down",
                "severity": "High",
                "message": f"Interface line protocol/status is down: \"{ln}\""
            })
    return issues


def check_duplicate_ip(text):
    issues = []
    seen = {}
    for ln in _lines(text):
        for ip in IP_RE.findall(ln):
            # skip obvious masks/broadcast-ish noise picked up alongside IPs
            if ip.startswith("255.") or ip == "0.0.0.0":
                continue
            seen.setdefault(ip, []).append(ln)
    for ip, lines_found in seen.items():
        if len(lines_found) > 1:
            # only flag if it looks like it's assigned on more than one distinct line
            distinct = set(lines_found)
            if len(distinct) > 1:
                issues.append({
                    "type": "duplicate_ip",
                    "severity": "High",
                    "message": f"IP address {ip} appears in more than one place in the evidence - check for a duplicate assignment: " +
                                " | ".join(list(distinct)[:3])
                })
    return issues


def check_gateway_mismatch(text):
    issues = []
    gw_match = re.search(r"default gateway[:\s]+(\d{1,3}\.\d{1,3}\.\d{1,3}\.\d{1,3})", text, re.IGNORECASE)
    if not gw_match:
        return issues
    gateway_ip = gw_match.group(1)

    # collect candidate router/interface IPs from "show ip interface brief"-style lines
    interface_ips = set()
    for ln in _lines(text):
        if re.match(r"^(fa|gi|se|vlan|eth|loopback)", ln, re.IGNORECASE) or "interface" in ln.lower():
            interface_ips.update(IP_RE.findall(ln))

    if interface_ips and gateway_ip not in interface_ips:
        issues.append({
            "type": "gateway_mismatch",
            "severity": "High",
            "message": f"PC's default gateway {gateway_ip} does not match any router/SVI interface IP found in the evidence "
                        f"({', '.join(sorted(interface_ips))}). Check gateway config or VLAN assignment."
        })
    return issues


def check_mask_mismatch(text):
    issues = []
    # pull (ip, mask) pairs that appear close together on the same line
    pairs = []
    for ln in _lines(text):
        ips = IP_RE.findall(ln)
        masks = MASK_RE.findall(ln)
        if ips and masks:
            pairs.append((ips[0], masks[0], ln))

    # group by /24-ish network guess using the reported mask, then compare masks
    # within lines that mention the same VLAN or subnet octets
    by_subnet_prefix = {}
    for ip, mask, ln in pairs:
        try:
            iface = ipaddress.ip_interface(f"{ip}/{mask}")
            key = str(iface.network.network_address).rsplit(".", 1)[0]  # first 3 octets as a loose grouping key
        except ValueError:
            continue
        by_subnet_prefix.setdefault(key, []).append((ip, mask, ln))

    for key, entries in by_subnet_prefix.items():
        masks_used = {m for _, m, _ in entries}
        if len(masks_used) > 1:
            detail = "; ".join(f"{ip}/{mask}" for ip, mask, _ in entries)
            issues.append({
                "type": "mask_mismatch",
                "severity": "Medium",
                "message": f"Interfaces in the same {key}.x range report different subnet masks: {detail}"
            })
    return issues


def check_missing_vlan(text, concept_tag="", symptom=""):
    issues = []
    mentioned = set(VLAN_RE.findall((concept_tag or "") + " " + (symptom or "")))
    if not mentioned:
        return issues

    vlan_table_section = ""
    lower = text.lower()
    if "show vlan" in lower:
        idx = lower.find("show vlan")
        vlan_table_section = text[idx: idx + 800]

    listed_vlans = set(re.findall(r"^\s*(\d{1,4})\s+\S", vlan_table_section, re.MULTILINE))

    for vlan_id in mentioned:
        if "show vlan" in lower and vlan_id not in listed_vlans:
            issues.append({
                "type": "missing_vlan",
                "severity": "High",
                "message": f"VLAN {vlan_id} is referenced in the case but not listed in the 'show vlan brief' evidence provided."
            })
    return issues


def check_missing_route(text, expected_fault=""):
    issues = []
    lower = text.lower()
    if "route" not in (expected_fault or "").lower():
        return issues
    if "show ip route" not in lower:
        issues.append({
            "type": "missing_route",
            "severity": "Medium",
            "message": "Expected fault mentions routing, but no 'show ip route' output was included as evidence - ask for it before confirming the diagnosis."
        })
        return issues

    idx = lower.find("show ip route")
    route_section = text[idx: idx + 1000]
    networks_mentioned = set(re.findall(r"\b(\d{1,3}\.\d{1,3}\.\d{1,3}\.0)\b", text[:idx]))
    for net in networks_mentioned:
        if net not in route_section:
            issues.append({
                "type": "missing_route",
                "severity": "Medium",
                "message": f"Network {net}/24 is mentioned earlier in the case but does not appear in the 'show ip route' table."
            })
    return issues


def run_checks(show_output, expected_fault="", concept_tag="", symptom=""):
    """Run every check and return a flat list of issue dicts."""
    if not show_output or not show_output.strip():
        return [{
            "type": "no_evidence",
            "severity": "Low",
            "message": "No show-command output was provided for this case, so the rule checker has nothing to scan."
        }]

    issues = []
    issues += check_interface_down(show_output)
    issues += check_duplicate_ip(show_output)
    issues += check_gateway_mismatch(show_output)
    issues += check_mask_mismatch(show_output)
    issues += check_missing_vlan(show_output, concept_tag, symptom)
    issues += check_missing_route(show_output, expected_fault)
    return issues


def run_checks_json(show_output, expected_fault="", concept_tag="", symptom=""):
    return json.dumps(run_checks(show_output, expected_fault, concept_tag, symptom))


# quick manual test when you run this file directly:
# python rule_checker.py
if __name__ == "__main__":
    sample = """
    PC1 default gateway: 192.168.30.1
    FastEthernet0/1 is up, line protocol is up
      Internet address is 192.168.30.1/24
    FastEthernet0/2 is administratively down, line protocol is down
      Internet address is 192.168.30.1/24
    show vlan brief
    VLAN Name                             Status    Ports
    10   Sales                            active    Fa0/1, Fa0/2
    show ip route
    192.168.10.0/24 is directly connected
    """
    for issue in run_checks(sample, expected_fault="VLAN issue", concept_tag="vlan 30", symptom="cannot reach vlan 30"):
        print(f"[{issue['severity']}] {issue['type']}: {issue['message']}")
