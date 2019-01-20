from urllib.parse import parse_qs
import secrets
import json
import os

from datetime import datetime
from sanic import Blueprint, response
from sanic_cors import CORS
from pymongo.errors import DuplicateKeyError
from pymongo import ReturnDocument

from .utils import auth_required, config, validate_github_payload, log_server_update, log_server_stop, Github

from dhooks import Embed

domain = config.DOMAIN
host = f'api.{domain}'

api = Blueprint('api', host=host)

CORS(api, automatic_options=True)


@api.get('/')
async def index(request):
    endpoints = set()
    deprecated = set()

    for name, (route, handler) in request.app.router.routes_names.items():
        if name.startswith('api.') or name.startswith('modmail.') or route.startswith('api.'):
            route = route.replace('api.modmail.tk', '')
            if route in ['/', '/api/']:
                continue
            if route.startswith('api.kybr.tk'):
                deprecated.add(route)
            else:
                endpoints.add(route)

    resp = {'success': True, 'endpoints': list(endpoints), 'deprecated': list(deprecated)}

    return response.text(json.dumps(resp, indent=4))


@api.post('/webhooks/github')
async def upgrade(request):
    if not validate_github_payload(request):
        return response.text('fuck off', 401)  # not sent by github
    request.app.loop.create_task(restart_later(request.app))
    return response.json({'success': True})


@api.get('/badges/instances.svg')
async def badges_instances(request):
    instances = await request.app.db.users.count_documents({})
    url = f"https://img.shields.io/badge/instances-{instances}-green.svg?style=for-the-badge"
    async with request.app.session.get(url) as resp:
        file = await resp.read()
    return response.raw(file, content_type='image/svg+xml', headers={'cache-control': 'no-cache'})


@api.get('/logs/key')
@auth_required()
async def get_log_url(request, auth_info):
    # payload should have discord_uid, channel_id, guild_id
    user_id = auth_info['user_id']
    while True:
        key = secrets.token_hex(6)
        try:
            await request.app.db.logs.insert_one(
                {
                    '_id': key,
                    'key': key,
                    'open': True,
                    'created_at': str(datetime.utcnow()),
                    'closed_at': None,
                    'user_id': user_id,
                    'channel_id': request.json['channel_id'],
                    'guild_id': request.json['guild_id'],
                    'creator': request.json['creator'],
                    'recipient': request.json['recipient'],
                    'closer': None,
                    'messages': [],
                }
            )
        except DuplicateKeyError:
            continue
        else:
            await request.app.db.api.find_one_and_update({'user_id': user_id}, {'$push': {'logs': request.json['channel_id']}})
            return response.text(f'https://logs.modmail.tk/{key}')


@api.get('/logs/user/<recipient_id>')
@auth_required()
async def get_logs_user(request, auth_info, recipient_id):
    """Get logs by recipient discord ID"""
    user_id = auth_info['user_id']
    recipient_logs = await request.app.db.logs.find({'recipient.id': recipient_id, 'user_id': user_id}).to_list(None)
    return response.json(recipient_logs)


@api.get('/logs/<channel_id>')
@auth_required()
async def get_log_data(request, auth_info, channel_id):
    """Get log data"""
    user_id = auth_info['user_id']
    if channel_id in auth_info['logs']:
        return await request.app.db.logs.find_one({'channel_id': channel_id, 'user_id': user_id})
    else:
        return response.text('Not Found', status=404)



@api.post('/logs/<channel_id>')
@auth_required()
async def post_log(request, auth_info, channel_id):
    """Replaces the content"""
    user_id = auth_info['user_id']
    if channel_id in auth_info['logs']:
        log = await request.app.db.logs.find_one_and_update(
            {'channel_id': channel_id, 'user_id': user_id},
            {'$set': {i: request.json[i] for i in request.json}},
            return_document=ReturnDocument.AFTER
        )
        return response.json(log)
    else:
        return response.text('Not Found', status=404)


@api.patch('/logs/<channel_id>')
@auth_required()
async def patch_log_content(request, auth_info, channel_id):
    """Appends the content"""
    user_id = auth_info['user_id']

    if channel_id in auth_info['logs']:
        log = await request.app.db.logs.find_one_and_update(
            {'channel_id': channel_id, 'user_id': user_id},
            {'$push': {'messages': request.json['payload']}},
            return_document=ReturnDocument.AFTER
        )
        return response.json(log)
    else:
        return response.text('Not Found', status=404)


@api.delete('/logs/<channel_id>')
@auth_required()
async def delete_log(request, auth_info, channel_id):
    """Delete log"""
    user_id = auth_info['user_id']
    if channel_id in auth_info['logs']:
        await request.app.db.logs.find_one_and_delete(
            {'channel_id': channel_id, 'user_id': user_id}
        )
        await request.app.db.api.find_one_and_update(
            {'user_id': user_id},
            {'$pull': {'logs': channel_id}}
        )
        return response.text('', status=204)
    else:
        return response.text('Not Found', status=404)


@api.get('/config')
@auth_required()
async def get_config(request, auth_info):
    """Get config data"""
    return response.json(auth_info['config'])


@api.patch('/config')
@auth_required()
async def update_config(request, auth_info):
    user_id = auth_info['user_id']
    await request.app.db.api.update_one({'user_id': user_id}, {'$set': {'config': request.json}})
    return response.json({'success': True})


@api.get('/metadata')
async def get_modmail_info(request):
    app = request.app

    resp = await app.session.get('https://raw.githubusercontent.com/kyb3r/modmail/master/bot.py')
    text = await resp.text()

    version = text.splitlines()[24].split(' = ')[1].strip("'")

    data = {
        'latest_version': version,
        'instances': await app.db.users.count_documents({})
    }
    return response.json(data)

async def log_new_instance(request):
    data = request.json
    count = await request.app.db.users.count_documents({})

    em = Embed(color=0x36393F, timestamp='now')
    em.add_field(name='Guild Name', value=data['guild_name'])
    em.add_field(name='Member Count', value=data['member_count'])
    em.add_field(name='Owner', value=f"<@{data['owner_id']}>")
    em.set_footer(text=f"{count} - v{data['version']}", icon_url=data.get('avatar_url'))
    
    await request.app.new_instance_webhook.send(
        embed=em, 
        username='New Server', 
        avatar_url='https://i.imgur.com/klWk4Si.png'
        )

@api.post('/metadata')
async def update_modmail_data(request):
    data = request.json

    valid_keys = (
        'guild_id', 'guild_name', 'member_count',
        'uptime', 'version', 'bot_id', 'bot_name',
        'latency', 'owner_name', 'owner_id', 'selfhosted',
        'last_updated', 'avatar_url'
    )

    if any(k not in valid_keys for k in data):
        return response.json({'message': 'invalid payload'}, 401)

    
    exists = await request.app.db.users.find_one({
        'guild_id': data['guild_id'],
        'bot_id': data['bot_id']
        })

    if exists is None:
        await log_new_instance(request)

    await request.app.db.users.update_one(
        {'bot_id': data['bot_id']},
        {'$set': data},
        upsert=True
    )

    if request.token:
        user = await request.app.db.api.find_one({'token': request.token})
        if user is not None:
            await request.app.db.api.update_one(
                {'token': request.token},
                {'$set': {'metadata': data}}
            )

    return response.json({'success': 'true'})


# GET - Get token
# PATCH - Regen token
# POST - Create token
@api.get('/token')
@auth_required()
async def get_token_info(request, auth_info):
    auth_info.pop('_id')
    return response.json(auth_info)


@api.get('/token/verify')
@auth_required()
async def verify_token(request, user):
    return response.json({'success': True})


@api.patch('/token')
@auth_required()
async def regen_token(request, auth_info):
    new_token = secrets.token_hex(15)
    request['session']['token'] = new_token
    new_data = await request.app.db.api.find_one_and_update(
        {'user_id': auth_info['user_id']},
        {'$set': {'token': new_token}},
        return_document=ReturnDocument.AFTER
    )
    new_data.pop('_id')
    return response.json(new_data)


@api.get('/github/update')
@auth_required()
async def modmail_github_check(request, user):
    user = await Github.login(request.app, user['github_access_token'])
    data = await user.update_repository()
    return response.json({
        'error': False,
        'message': 'Updated modmail.',
        'user': {
            'username': user.username,
            'avatar_url': user.avatar_url,
            'url': user.url
        },
        'data': data
    })


@api.get('/github/userinfo')
@auth_required()
async def modmail_github_user(request, user):
    if user is None:
        return response.json({'error': True, 'message': 'Unable to find user. Please go through OAuth.'}, status=403)
    else:
        user = await Github.login(request.app, user['github_access_token'])
        return response.json({
            'error': False,
            'message': 'User data retrieved.',
            'user': {
                'username': user.username,
                'avatar_url': user.avatar_url,
                'url': user.url
            }
        })


@api.put('/star')
@auth_required()
async def star_repo(request, auth_info):
    user = await Github.login(request.app, auth_info['github_access_token'])
    await user.star_repository()
    return response.text('', status=204)


async def restart_later(app):
    await log_server_update(app)
    await log_server_stop(app)
    await app.session.close()
    command = 'git pull && pm2 restart kyb3r.tk'
    os.system(f'echo {app.password}|sudo -S {command}')
