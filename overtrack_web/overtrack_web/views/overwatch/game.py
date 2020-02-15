import json
import logging
import string
import warnings
from itertools import chain
from typing import Sequence, List, Optional
from urllib.parse import urlparse

import boto3
import requests
from dataclasses import asdict, fields, is_dataclass
from flask import Blueprint, Request, render_template, request, url_for
from overtrack_models.dataclasses.overwatch.basic_types import Map, Hero
from overtrack_models.dataclasses.overwatch.overwatch_game import OverwatchGame
from overtrack_models.dataclasses.overwatch.statistics import HeroStats

from overtrack_models.dataclasses.typedload import referenced_typedload
from overtrack_models.orm.overwatch_game_summary import OverwatchGameSummary
from overtrack_web.data import overwatch_data
from overtrack_web.lib.authentication import check_authentication
from overtrack_web.lib.session import session
from overtrack_web.views.overwatch import OLDEST_SUPPORTED_GAME_VERSION, sr_change

GAMES_BUCKET = 'overtrack-overwatch-games'


request: Request = request
logger = logging.getLogger(__name__)
try:
    s3 = boto3.client('s3')
    """ :type s3: mypy_boto3.s3.Client """
    s3.list_objects_v2(Bucket=GAMES_BUCKET)
except:
    logger.exception('Failed to create AWS S3 client - using HTTP for downloading games')
    s3 = None
try:
    logs = boto3.client('logs')
    """ :type s3: boto3_type_annotations.s3.Client """
except:
    logger.exception('Failed to create AWS logs client - running without admin logs')
    logs = None

game_blueprint = Blueprint('overwatch.game', __name__)


@game_blueprint.context_processor
def context_processor():

    def ability_is_ult(ability) -> bool:
        if not ability:
            return False
        hero_name, ability_name = ability.split('.')
        hero = overwatch_data.heroes.get(hero_name)
        if not hero:
            warnings.warn(f'Could not get hero data for {hero_name}', RuntimeWarning)
            return False
        if not hero.ult:
            warnings.warn(f'Hero {hero_name} does not have ult defined', RuntimeWarning)
            return False
        return ability_name == hero.ult

    def sort_stats(stats: Sequence[HeroStats]) -> List[HeroStats]:
        stats = sorted(list(stats), key=lambda s: (s.hero != 'all heroes', s.hero))
        all_heroes_duration = stats[0].time_played
        stats = [s for s in stats if s.time_played / all_heroes_duration > 0.25]

        heroes = [s.hero for s in stats]
        if 'all heroes' in heroes and len(heroes) == 2:
            # only 'all heroes' and one other
            stats = [s for s in stats if s.hero != 'all heroes']

        return stats

    def get_stat_type(hero_name: str, stat_name: str) -> str:
        if hero_name in overwatch_data.heroes:
            hero = overwatch_data.heroes[hero_name]
            stats_by_name = {
                s.name: s
                for s in chain(*hero.stats)
            }
            if stat_name in stats_by_name:
                stat = stats_by_name[stat_name]
                if stat.stat_type == 'maximum':
                    return 'value'
                if stat.stat_type == 'average':
                    return 'average'
                elif stat.stat_type == 'best':
                    return 'best'
                else:
                    logger.error(f"Don't know how to handle stat type {stat.stat_type!r} for {hero_name}: {stat_name!r}")
                    return 'value'

        logger.error(f"Couldn't get stat type for {hero_name}: {stat_name!r}")
        return 'value'

    return {
        'game_name': 'overwatch',

        'sr_change': sr_change,
        'ability_is_ult': ability_is_ult,
        'sort_stats': sort_stats,
        'get_stat_type': get_stat_type,

        'asdict': asdict,
    }


@game_blueprint.app_template_filter('map_jumbo_style')
def map_jumbo_style(map_: Map):
    map_name = map_.name.lower().replace(' ', '-')
    map_name = ''.join(c for c in map_name if c in (string.digits + string.ascii_letters + '-'))
    return (
        f'background-image: linear-gradient(#222854 0px, rgba(0, 0, 0, 0.42) 100%), '
        f'url({url_for("static", filename="images/overwatch/map_banners/" + map_name + ".jpg")}); '
        f'background-color: #222854;'
    )


@game_blueprint.app_template_filter('format_number')
def format_number(v: Optional[float], precision: Optional[float] = 1):
    if v is None:
        return v
    if precision is None:
        if v > 500:
            precision = 0
        else:
            precision = 1
    return f'{v:,.{precision}f}'


@game_blueprint.app_template_filter('hero_name')
def hero_name(h: str):
    if h in overwatch_data.heroes:
        return overwatch_data.heroes[h].name
    else:
        return h.title()


@game_blueprint.route('/<path:key>')
def game(key: str):
    summary = OverwatchGameSummary.get(key)

    game = load_game(summary)
    game.timestamp = summary.time

    summary_dict = summary.asdict()
    summary_dict['key'] = (summary.key, f'https://overtrack-overwatch-games.s3.amazonaws.com/{summary.key}.json')

    if check_authentication() is None and session.user.superuser:
        try:
            frames_url = urlparse(summary.frames_uri)
            signed_url = s3.generate_presigned_url(
                'get_object',
                Params={
                    'Bucket': frames_url.netloc,
                    'Key': frames_url.path[1:]
                }
            )
            summary_dict['frames_uri'] = (summary.frames_uri, signed_url)
        except:
            pass

    game_dict = {}
    for f in fields(game):
        if not is_dataclass(getattr(game, f.name)):
            game_dict[f.name] = getattr(game, f.name)
    game_dict['key'] = (summary.key, f'https://overtrack-overwatch-games.s3.amazonaws.com/{summary.key}.json')

    return render_template(
        'overwatch/game/game.html',

        summary=summary,
        game=game,

        summary_dict=summary_dict,
        game_dict=game_dict,

        all_stats=asdict(game.stats.stats['all heroes']) if 'all heroes' in game.stats.stats else {},

        OLDEST_SUPPORTED_GAME_VERSION=OLDEST_SUPPORTED_GAME_VERSION,
    )


def load_game(summary: OverwatchGameSummary) -> OverwatchGame:
    try:
        game_object = s3.get_object(
            Bucket=GAMES_BUCKET,
            Key=summary.key + '.json'
        )
        game_data = json.loads(game_object['Body'].read())
    except:
        game_object = None
        if s3:
            logger.exception('Failed to fetch game data from S3 - trying HTTP')
        r = requests.get(f'https://overtrack-overwatch-games.s3.amazonaws.com/{summary.key}.json')
        r.raise_for_status()
        game_data = r.json()

    return referenced_typedload.load(game_data, OverwatchGame)
