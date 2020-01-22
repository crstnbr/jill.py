from .defaults import default_scheme_ports
from .defaults import SOURCE_CONFIGFILE

from .net_utils import query_ip, port_response_time
from .filters import generate_info

from itertools import chain, repeat
from urllib.parse import urlparse
from string import Template

import requests
import json
import os
import logging

from typing import Optional, List

from requests.exceptions import RequestException


class ReleaseSource:
    def __init__(self,
                 name: str,
                 urls: List[str],
                 latest_urls: List[str],
                 timeout=2.0):
        # seperate stable and nightly versions because:
        #   * JuliaComputing stores them in two different s3 buckets
        #   * not all mirror servers need/want to serve nightly releases
        # store multiple urls because:
        #   * many mirrors have different multiple domains for different
        #     network choices, e.g., ipv4, ipv6, cernet, chinanet, rsync
        self.name = name
        self.url_templates = [Template(x) for x in urls]
        # TODO: make latest an optional config
        self.latest_url_templates = [Template(x) for x in latest_urls]
        self.timeout = timeout
        self._latencies = dict()  # type: ignore

    @property
    def urls(self):
        return [x.template for x in self.url_templates]

    @property
    def latest_urls(self):
        return [x.template for x in self.latest_url_templates]

    @property
    def hosts(self):
        return [urlparse(url).netloc for url in self.urls]

    @property
    def latest_hosts(self):
        return [urlparse(url).netloc for url in self.latest_urls]

    @property
    def latencies(self):
        # only check latency once and lazily
        if not self._latencies:
            url_list = self.urls.copy()
            url_list.extend(self.latest_urls)
            host_list = [urlparse(url).netloc for url in url_list]

            # hosts might point to the same ip address
            host_ip_records = {host: query_ip(host) for host in host_list}
            ip_port_records = {host_ip_records[urlparse(url).netloc]:
                               default_scheme_ports[urlparse(url).scheme]
                               for url in url_list}

            latency_records = dict()
            # TODO: use threads
            for ip, port in ip_port_records.items():
                latency_records[ip] = port_response_time(ip, port,
                                                         self.timeout)
            self._latencies = {host: latency_records[host_ip_records[host]]
                               for host in host_list}
        return self._latencies

    def __repr__(self):
        return f"ReleaseSource({self.name})"

    def get_url(self, plain_version, system, architecture):
        """
        return one potential downloading url with minal network latency for
        specific version, system and architecture. Special version name such
        as 'latest' are treated differently.
        """
        if plain_version == "latest":
            template_lists = self.latest_url_templates
        else:
            template_lists = self.url_templates
        configs = generate_info(plain_version, system, architecture)
        url_list = [t.substitute(**configs) for t in template_lists]
        url_list.sort(key=lambda url:
                      self.latencies[urlparse(url).netloc])
        return url_list[0]


class SourceRegistry:
    def __init__(self, configfile=SOURCE_CONFIGFILE, timeout=2):
        self.configfile = configfile
        self.timeout = timeout

        with open(self.configfile, 'r') as f:
            upstream_records = json.load(f).get("upstream", {})
            registry = {k: ReleaseSource(**v)
                        for k, v in upstream_records.items()}
        self.registry = registry

    def __len__(self):
        return len(self.registry)

    def info(self):
        msg = f"found {len(self)} release sources:\n"
        for src in self.registry.keys():
            msg += "    - " + str(src)
        logging.info(msg)

    @property
    def latencies(self):
        records = dict()
        for src in self.registry.values():
            records.update(src.latencies)
        return records

    def get_urls(self, plain_version, system, architecture):
        """
        return a list of potential downloading urls for specific version,
        system and architecture. Special version name such as 'latest' are
        treated differently.
        """
        url_list = [src.get_url(plain_version, system, architecture)
                    for src in self.registry.values()]
        url_list.sort(key=lambda url:
                      self.latencies[urlparse(url).netloc])
        return url_list


default_registry = SourceRegistry()


def is_url_available(url, timeout):
    try:
        logging.debug(f"try {url}")
        r = requests.head(url, timeout=timeout)
    except RequestException as e:
        logging.debug(f"failed: {str(e)}")
        return False

    if r.status_code//100 == 4:
        logging.debug(f"failed: {r.status_code} error")
        return False
    elif r.status_code == 301 or r.status_code == 302:
        # redirect
        new_url = r.headers['Location']
        return is_url_available(new_url, timeout)
    else:
        return True


def query_download_url(version, system, arch, max_try=3, timeout=10):
    """
    return a valid download url to nearest mirror server. If there isn't
    such version then return None.
    """
    url_list = default_registry.get_urls(version, system, arch)

    url_list = chain.from_iterable(repeat(url_list, max_try))
    for url in url_list:
        if is_url_available(url, timeout):
            return url
    return None
