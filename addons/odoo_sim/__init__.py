from . import controllers
from . import models


def _genesis(env):
    env['game.world']._genesis()
    env['game.bank.account']._company_account()
