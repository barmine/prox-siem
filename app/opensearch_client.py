from opensearchpy import OpenSearch


def make_client(url: str) -> OpenSearch:
    return OpenSearch(hosts=[url], use_ssl=False, verify_certs=False)
