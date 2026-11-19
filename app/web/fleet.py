"""Fleet customers: contracts, vehicles, maintenance due, monthly acts."""
from __future__ import annotations

import re

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import RedirectResponse
from sqlmodel import Session

from ..db import get_session
from ..models import Client, FleetContract, utcnow
from ..services import fleet as fsvc
from ..tenancy import UserContext, get_scoped, scoped
from .deps import check, render, require

router = APIRouter()


@router.get("/fleet")
def fleet_list(request: Request, s: Session = Depends(get_session),
               ctx: UserContext = Depends(require("fleet.view"))):
    contracts = list(s.exec(scoped(FleetContract, ctx).order_by(FleetContract.name)).all())
    return render(request, s, ctx, "fleet_list.html", rows=fsvc.contracts_overview(s, contracts))


@router.get("/fleet/{cid}")
def fleet_contract(cid: int, request: Request, s: Session = Depends(get_session),
                   ctx: UserContext = Depends(require("fleet.view"))):
    c = get_scoped(s, FleetContract, cid, ctx)
    client = s.get(Client, c.client_id)
    return render(request, s, ctx, "fleet_contract.html", c=c, client=client,
                  ov=fsvc.contract_detail(s, c), acts=fsvc.list_acts(s, c),
                  default_period=utcnow().strftime("%Y-%m"),
                  can_acts=ctx.can("fleet.acts", c.branch_id))


@router.post("/fleet/{cid}/act")
def fleet_make_act(cid: int, period: str = Form(""), s: Session = Depends(get_session),
                   ctx: UserContext = Depends(require("fleet.acts"))):
    c = get_scoped(s, FleetContract, cid, ctx)
    check(ctx, "fleet.acts", c.branch_id)
    if not re.fullmatch(r"20\d\d-(0[1-9]|1[0-2])", period):
        return RedirectResponse(f"/fleet/{cid}?msg=err.bad_period", status_code=303)
    fsvc.build_monthly_act(s, c, period)
    s.commit()
    return RedirectResponse(f"/fleet/{cid}?msg=msg.act_created", status_code=303)
