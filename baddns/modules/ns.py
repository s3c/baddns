from baddns.base import BadDNS_base

from baddns.lib.dnsmanager import DNSManager
from baddns.lib.dnswalk import DnsWalk

import logging

log = logging.getLogger(__name__)


class BadDNS_ns(BadDNS_base):
    name = "NS"
    description = "Check for dangling NS records, and interrogate them for domain takeover opportunities"

    def __init__(self, target, **kwargs):
        super().__init__(target, **kwargs)
        self.target_dnsmanager = DNSManager(
            target, dns_client=self.dns_client, custom_nameservers=self.custom_nameservers
        )

    async def dispatch(self):
        # omit everything except CNAME. If there is a CNAME chain, we want to run against the end of it.
        await self.target_dnsmanager.dispatchDNS(omit_types=[["A", "AAAA", "MX", "NS", "SOA", "TXT"]])

        if self.target_dnsmanager.answers["CNAME"] != None:
            log.warning(
                f"Detected CNAME(S). Will set target to end of CNAME chain: [{self.target_dnsmanager.answers['CNAME'][-1]}]"
            )
            self.target_dnsmanager.target = self.target_dnsmanager.answers["CNAME"][-1]
            self.target = self.target_dnsmanager.answers["CNAME"][-1]
            self.target_dnsmanager.reset_answers()

        await self.target_dnsmanager.dispatchDNS(omit_types=["CNAME", "NS"])

        dnswalk = DnsWalk(self.target_dnsmanager)
        self.target_dnsmanager.answers["NS"] = await dnswalk.ns_trace(self.target)
        return True

    @staticmethod
    def get_substring_matches(nameservers, strings):
        matched_nameservers = set()
        matched_signatures = set()

        for ns in nameservers:
            for s in strings:
                if s in ns:
                    matched_nameservers.add(ns)
                    matched_signatures.add(s)

        if not matched_nameservers and not matched_signatures:
            return None

        return list(matched_nameservers), list(matched_signatures)

    def analyze(self):
        log.debug("Staring analysis")
        findings = []
        if len(self.target_dnsmanager.answers["NS"]) > 0:
            target_nameservers = self.target_dnsmanager.answers["NS"]
            log.debug("Nameserver(s) found. Continuing...")
        else:
            return False
        if self.target_dnsmanager.answers["SOA"] == None:
            log.debug("No SOA record found w/nameservers present")

            signature_name = "GENERIC"
            matching_signatures = None
            r = None
            for sig in self.signatures:
                if sig.signature["mode"] == "dns_nosoa":
                    sig_nameservers = [ns for ns in sig.signature["identifiers"]["nameservers"]]
                    r = self.get_substring_matches(target_nameservers, sig_nameservers)
                    if r:
                        matching_signatures = r[1]
                        signature_name = sig.signature["service_name"]
                        log.debug(
                            f"Found match for for target nameservers {', '.join(target_nameservers)} with signature [{sig.signature['service_name']}] "
                        )
                        break

            findings.append(
                {
                    "target": self.target_dnsmanager.target,
                    "nameservers": target_nameservers,
                    "signature_name": signature_name,
                    "matching_signatures": matching_signatures,
                    "technique": "NS RECORD WITHOUT SOA",
                }
            )

        return findings
