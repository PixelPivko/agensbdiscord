import json
from types import SimpleNamespace
from unittest.mock import AsyncMock
import pytest
from cogs.tickets import KINDS, questions, Panel, Continue, Controls, Form, Tickets, SCHEMA
from services.database import Database

@pytest.mark.asyncio
async def test_persistent_buttons_and_form_limits():
    cog = SimpleNamespace()
    assert len(Panel(cog).children) == 7
    for cls in (Panel,Continue,Controls):
        assert cls(cog).is_persistent()
    for kind in KINDS:
        qs = questions(kind)
        assert all(len(q)<=45 for q in qs)
        for offset in range(0,len(qs),5):
            form = Form(cog,kind,offset)
            assert 1 <= len(form.children) <= 5
    assert len(questions('appeal')) == 7
    assert len(questions('mrp')) == 10

@pytest.mark.asyncio
async def test_drafts_survive_restart_and_are_guild_scoped(tmp_path):
    path = str(tmp_path/'tickets.db')
    db = Database(path)
    await db.open()
    await db.execute(SCHEMA)
    for gid in (1,2):
        await db.execute('INSERT INTO tickets(guild_id,user_id,kind,answers,updated_at) VALUES(?,?,?,?,?)',
                         (gid,42,'appeal',json.dumps([str(gid)]),1))
    await db.close()
    await db.open()
    cog=Tickets(SimpleNamespace(db=db))
    assert json.loads((await cog.state(1,42))['answers']) == ['1']
    assert json.loads((await cog.state(2,42))['answers']) == ['2']
    await db.close()


from cogs.tickets import reviewer_roles, server_for, can_review

@pytest.mark.parametrize('server', ['mrp','classic','discord'])
def test_exact_routing(config, server):
    cfg = next(iter(config['guilds'].values()))['tickets']
    assert reviewer_roles(cfg,'admin',server) == set(cfg['routing']['admin'][server])
    assert reviewer_roles(cfg,'vmn',server) == {1546592219438522589}
    for kind in ('player','appeal',server):
        assert reviewer_roles(cfg,kind,server) == set(cfg['routing']['common']) | set(cfg['routing']['servers'][server])
    assert server_for(server,[]) == server
    assert server_for('appeal',[server.upper()]) == server


def test_invalid_server_rejected():
    with pytest.raises(ValueError):
        server_for('player',['all'])


def test_claim_authorization(config):
    cfg = next(iter(config['guilds'].values()))['tickets']
    row = {'user_id':42,'kind':'admin','answers':json.dumps(['MRP'])}
    member = SimpleNamespace(id=55,guild=SimpleNamespace(owner_id=1),
        guild_permissions=SimpleNamespace(administrator=False),roles=[SimpleNamespace(id=1562921243081179186)])
    assert not can_review(member,row,cfg)  # regular MRP staff cannot process admin complaints
    member.roles=[SimpleNamespace(id=1562914244972904528)]
    assert can_review(member,row,cfg)
    member.id=42
    assert not can_review(member,row,cfg)  # cannot claim own complaint
