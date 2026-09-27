"""rs.ge-ს ოფიციალური SOAP სერვისები (სადაც არსებობს, ისინი სჯობს ბრაუზერის ავტომატიზაციას).

WayBillService (ზედნადებები) მუშაობს "სერვისის მომხმარებლით" (su/sp), რომელსაც გადამხდელი
თავად ქმნის eservice.rs.ge-ზე. ამ მომხმარებელს SMS/CAPTCHA არ სჭირდება და მისი უფლებები
შეზღუდულია მხოლოდ სერვისებით - ამიტომ ზედნადებების/ანგარიშ-ფაქტურების სინქრონიზაციისთვის
ეს არის სწორი, ოფიციალური გზა.

⚠️ ოპერაციებისა და პარამეტრების სახელები აღებულია სერვისის საჯარო დოკუმენტაციიდან და
გადასამოწმებელია WSDL-თან (`python -m zeep <WSDL_URL>` აჩვენებს ზუსტ სიგნატურებს).
ამ რეპოზიტორიის CI გარემოდან rs.ge მიუწვდომელია, ამიტომ ეს მოდული unit-ტესტით არ მოწმდება.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

WAYBILL_WSDL = "https://services.rs.ge/WayBillService/WayBillService.asmx?WSDL"


@dataclass(frozen=True)
class ServiceUser:
    username: str
    password: str = field(repr=False)


class RsGeSoapError(Exception):
    pass


def _element_to_dict(element) -> dict[str, Any] | str | None:
    """lxml ელემენტი (WayBillService ხშირად აბრუნებს XML-ს `any` ტიპად) -> dict."""
    if element is None:
        return None
    children = list(element)
    if not children:
        return element.text
    out: dict[str, Any] = {}
    for child in children:
        tag = child.tag.split("}")[-1]
        value = _element_to_dict(child)
        if tag in out:
            if not isinstance(out[tag], list):
                out[tag] = [out[tag]]
            out[tag].append(value)
        else:
            out[tag] = value
    return out


class WayBillClient:
    def __init__(self, user: ServiceUser, wsdl: str = WAYBILL_WSDL, timeout: int = 60):
        self._user = user
        self._wsdl = wsdl
        self._timeout = timeout
        self._client = None

    def _svc(self):
        if self._client is None:
            from zeep import Client, Settings
            from zeep.transports import Transport

            self._client = Client(self._wsdl, settings=Settings(strict=False, xml_huge_tree=True),
                                  transport=Transport(timeout=self._timeout, operation_timeout=self._timeout))
        return self._client.service

    async def _call(self, operation: str, **params) -> Any:
        def run():
            return getattr(self._svc(), operation)(su=self._user.username, sp=self._user.password, **params)

        try:
            return await asyncio.to_thread(run)
        except Exception as exc:  # zeep.exceptions.Fault / TransportError
            raise RsGeSoapError(f"{operation} failed: {exc}") from exc

    async def check_service_user(self) -> dict[str, Any]:
        """აბრუნებს, ვალიდურია თუ არა სერვისის მომხმარებელი (+ un_id / s_user_id)."""
        result = await self._call("chek_service_user")  # sic - ოპერაციის სახელი WSDL-შია ასე
        return dict(result) if hasattr(result, "__iter__") and not isinstance(result, (str, bool)) else {"ok": result}

    async def buyer_waybills(self, start: datetime, end: datetime) -> list[dict[str, Any]]:
        """შემოსული ზედნადებები (ჩვენ ვართ მყიდველი) პერიოდში."""
        raw = await self._call("get_buyer_waybills", create_date_s=start, create_date_e=end)
        return self._rows(raw)

    async def seller_waybills(self, start: datetime, end: datetime) -> list[dict[str, Any]]:
        """გაწერილი ზედნადებები (ჩვენ ვართ გამყიდველი) პერიოდში."""
        raw = await self._call("get_waybills", create_date_s=start, create_date_e=end)
        return self._rows(raw)

    @staticmethod
    def _rows(raw) -> list[dict[str, Any]]:
        data = _element_to_dict(raw) if hasattr(raw, "tag") else raw
        if not data:
            return []
        if isinstance(data, dict):
            rows = data.get("WAYBILL") or data.get("WAYBILL_LIST") or next(iter(data.values()), [])
            if isinstance(rows, dict):
                rows = rows.get("WAYBILL", rows)
            return rows if isinstance(rows, list) else [rows]
        return list(data)
