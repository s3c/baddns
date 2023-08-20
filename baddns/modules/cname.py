from datetime import date, datetime
from dateutil import parser as date_parser

from baddns.base import BadDNS_base

from baddns.lib.dnsmanager import DNSManager
from baddns.lib.httpmanager import HttpManager
from baddns.lib.whoismanager import WhoisManager
from baddns.lib.matcher import Matcher

import logging

log = logging.getLogger(__name__)


class BadDNS_cname(BadDNS_base):
    name = "CNAME"
    description = "Check for dangling CNAME records and interrogate them for subdomain takeover opportunities"

    def __init__(self, target, **kwargs):
        super().__init__(target, **kwargs)
        self.target_dnsmanager = DNSManager(target, dns_client=self.dns_client)
        self.target_httpmanager = None
        self.cname_dnsmanager = None
        self.cname_whoismanager = None

    @staticmethod
    def date_parse(unknown_date):
        # Check if it's already a datetime object
        if isinstance(unknown_date, datetime):
            return unknown_date

        # If it's a string, try to parse it
        if isinstance(unknown_date, str):
            try:
                return date_parser.parse(unknown_date)
            except ValueError as e:
                log.debug(f"Failed to parse date from string: {unknown_date}. Error: {e}")
                return None

        log.debug(f"Unsupported date object type: {type(unknown_date)}. Value: {unknown_date}")
        return None

    async def dispatch(self):
        await self.target_dnsmanager.dispatchDNS()

        if self.target_dnsmanager.answers["CNAME"] != None:
            log.info(
                f"Found CNAME(S) [{' -> '.join([self.target_dnsmanager.target] + self.target_dnsmanager.answers['CNAME'])}]"
            )
        else:
            log.info("No CNAME Found :/")
            return False

        self.cname_dnsmanager = DNSManager(self.target_dnsmanager.answers["CNAME"][-1], dns_client=self.dns_client)
        await self.cname_dnsmanager.dispatchDNS(omit_types=["CNAME"])

        # if the domain resolves, we can try for HTTP connections
        if not self.cname_dnsmanager.answers["NXDOMAIN"]:
            log.debug("CNAME resolved correctly, proceeding with HTTP dispatch")
            self.target_httpmanager = HttpManager(self.target, http_client_class=self.http_client_class)
            await self.target_httpmanager.dispatchHttp()
            log.debug("HTTP dispatch complete")
        # if the cname doesn't resolve, we still need to see if the base domain is unregistered
        # even if it is registered, we still use whois to check for expired domains
        log.debug("performing WHOIS lookup")
        self.cname_whoismanager = WhoisManager(self.target_dnsmanager.answers["CNAME"][-1])
        await self.cname_whoismanager.dispatchWHOIS()
        log.debug("WHOIS dispatch complete")
        return True

    def analyze(self):
        findings = []
        if self.cname_dnsmanager.answers["NXDOMAIN"]:
            signature_name = "Generic Dangling CNAME"
            matching_domain = None

            log.info(f"Got NXDOMAIN for CNAME {self.cname_dnsmanager.target}. Checking against signatures...")
            for sig in self.signatures:
                if sig.signature["mode"] == "dns_nxdomain":
                    log.debug(f"Trying signature {sig.signature['service_name']}")
                    sig_cnames = [c["value"] for c in sig.signature["identifiers"]["cnames"]]
                    for sig_cname in sig_cnames:
                        log.debug(f"Checking CNAME {self.cname_dnsmanager.target} against {sig_cname}")
                        if self.cname_dnsmanager.target.endswith(sig_cname):
                            log.debug(f"CNAME {self.cname_dnsmanager.target} Vulnerable ({sig_cname})")
                            signature_name = sig.signature["service_name"]
                            matching_domain = sig_cname
                            break

            findings.append(
                {
                    "target": self.target_dnsmanager.target,
                    "cnames": self.target_dnsmanager.answers["CNAME"],
                    "signature_name": signature_name,
                    "matching_domain": matching_domain,
                    "technique": "CNAME NXDOMAIN",
                }
            )

        else:
            log.debug("Starting HTTP analysis")

            http_results = [
                self.target_httpmanager.http_allowredirects_results,
                self.target_httpmanager.http_denyredirects_results,
                self.target_httpmanager.https_allowredirects_results,
                self.target_httpmanager.https_denyredirects_results,
            ]

            for sig in self.signatures:
                if sig.signature["mode"] == "http":
                    log.debug(f"Trying signature {sig.signature['service_name']}")
                    if len(sig.signature["identifiers"]["cnames"]) > 0:
                        log.debug(
                            f"Signature contains cnames [{sig.signature['identifiers']['cnames']}], checking them"
                        )
                        if not any(
                            cname_dict["value"] in self.target_dnsmanager.answers["CNAME"][-1]
                            for cname_dict in sig.signature["identifiers"]["cnames"]
                        ):
                            log.debug(
                                f"no match for {sig.signature['identifiers']['cnames']} for in {self.target_dnsmanager.answers['CNAME'][-1]}"
                            )
                            continue
                        log.debug("passed CNAME check")

                    if len(sig.signature["identifiers"]["ips"]) > 0:
                        log.debug(f"Signature contains ips [{sig.signature['identifiers']['ips']}], checking them")
                        if not any(
                            ip_signature in self.cname_dnsmanager.ips
                            for ip_signature in sig.signature["identifiers"]["ips"]
                        ):
                            log.debug(
                                f"no match for {sig.signature['identifiers']['ips']} for in {self.cname_dnsmanager.ips}"
                            )
                            continue
                        log.debug("passed IPS")

                    m = Matcher(sig.signature["matcher_rule"])
                    log.debug("Checking for HTTP matches")
                    if any(m.is_match(hr) for hr in http_results if hr is not None):
                        log.debug(f"CNAME {self.cname_dnsmanager.target} Vulnerable")
                        log.debug(f"With matcher_rule {sig.signature['matcher_rule']}")
                        findings.append(
                            {
                                "target": self.target_dnsmanager.target,
                                "cnames": self.target_dnsmanager.answers["CNAME"],
                                "signature_name": sig.signature["service_name"],
                                "technique": "HTTP String Match",
                            }
                        )

        # check whois data for expiring domains
        log.debug("analyzing whois results")
        if self.cname_whoismanager.whois_result:
            # check for unregistered CNAME
            if self.cname_whoismanager.whois_result["type"] == "error":
                log.debug("whois result was an error")
                if "No match for" in self.cname_whoismanager.whois_result["data"]:
                    findings.append(
                        {
                            "target": self.target_dnsmanager.target,
                            "cnames": self.target_dnsmanager.answers["CNAME"],
                            "signature_name": None,
                            "matching_domain": None,
                            "technique": "CNAME unregistered",
                        }
                    )

            # check for expired domain
            elif self.cname_whoismanager.whois_result["type"] == "response":
                log.debug("whois resulted in a response")
                expiration_data = self.cname_whoismanager.whois_result["data"]["expiration_date"]
                if isinstance(expiration_data, list):
                    expiration_date = expiration_data[0]
                else:
                    expiration_date = expiration_data

                expiration_date = self.date_parse(expiration_date)

                if expiration_date:
                    current_date = date.today()
                    if expiration_date.date() < current_date:
                        log.info(
                            f"Current Date ({current_date.strftime('%Y-%m-%d')}) after Expiration Date ({expiration_date.date().strftime('%Y-%m-%d')})"
                        )
                        findings.append(
                            {
                                "target": self.target_dnsmanager.target,
                                "cnames": self.target_dnsmanager.answers["CNAME"],
                                "signature_name": None,
                                "matching_domain": None,
                                "technique": "CNAME Base Domain Expired",
                                "expiration_date": expiration_date.strftime("%Y-%m-%d %H:%M:%S"),
                            }
                        )
                    else:
                        log.debug(f"Domain {self.cname_dnsmanager.target} is not expired")

        else:
            log.debug("whois_result was NoneType")

        return findings
