"""Google Docs API wrapper with native Tabs support."""
from typing import TypedDict

from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build


class TabSpec(TypedDict):
    title: str
    content: str


def make_doc_with_tabs(creds: Credentials, title: str, tabs: list[TabSpec]) -> dict:
    """Create a Google Doc with multiple native tabs.

    Flow:
      1. Create empty doc (Google auto-creates one tab inside it).
      2. Rename the auto-tab to ``tabs[0]`` and add ``tabs[1:]`` via addDocumentTab.
      3. Re-fetch doc to learn each tab's generated tabId.
      4. Insert content into each tab via insertText with location.tabId.
    """
    docs = build("docs", "v1", credentials=creds)

    doc = docs.documents().create(body={"title": title}).execute()
    doc_id = doc["documentId"]

    fetched = docs.documents().get(
        documentId=doc_id, includeTabsContent=True
    ).execute()
    first_tab_id = fetched["tabs"][0]["tabProperties"]["tabId"]

    structure_requests: list[dict] = [
        {
            "updateDocumentTabProperties": {
                "tabProperties": {
                    "tabId": first_tab_id,
                    "title": tabs[0]["title"],
                },
                "fields": "title",
            }
        }
    ]
    for tab in tabs[1:]:
        structure_requests.append(
            {"addDocumentTab": {"tabProperties": {"title": tab["title"]}}}
        )
    docs.documents().batchUpdate(
        documentId=doc_id, body={"requests": structure_requests}
    ).execute()

    fetched = docs.documents().get(
        documentId=doc_id, includeTabsContent=True
    ).execute()
    tab_resources = fetched.get("tabs", [])

    content_requests: list[dict] = []
    for tab_resource, tab_input in zip(tab_resources, tabs):
        tab_id = tab_resource["tabProperties"]["tabId"]
        content_requests.extend(
            render_content_to_requests(tab_input["content"], tab_id)
        )

    if content_requests:
        docs.documents().batchUpdate(
            documentId=doc_id, body={"requests": content_requests}
        ).execute()

    return {
        "doc_id": doc_id,
        "url": f"https://docs.google.com/document/d/{doc_id}/edit",
        "tabs": [
            {
                "title": t["tabProperties"]["title"],
                "tab_id": t["tabProperties"]["tabId"],
            }
            for t in tab_resources
        ],
    }


def render_content_to_requests(content: str, tab_id: str) -> list[dict]:
    """Convert tab content into Google Docs batchUpdate requests.

    Default: plain text insertion. Wave B will extend this to handle
    markdown (headings, bullets, bold/italic, code blocks).
    """
    return [
        {
            "insertText": {
                "location": {"tabId": tab_id, "index": 1},
                "text": content,
            }
        }
    ]
