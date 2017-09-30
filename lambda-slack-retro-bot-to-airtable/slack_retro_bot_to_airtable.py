"""Integration to send Slack messages when new code reviews are sent in Reviewable."""
import json
import os
import re
from datetime import datetime

from itertools import groupby
import requests

from airtable import airtable
from flask import abort, Flask, request, Response
from zappa.async import task

app = Flask(__name__)  # pylint: disable=invalid-name

_GOOD_CMDS = ('good',)
_BAD_CMDS = ('bad',)
_TRY_CMDS = ('try',)
_CATEGORY_CMDS = _GOOD_CMDS + _BAD_CMDS + _TRY_CMDS
_NEW_CMDS = ('new',)
_LIST_CMDS = ('list',)
_HELP_CMDS = ('help', '?')
_ALL_CMDS = _CATEGORY_CMDS + _NEW_CMDS + _LIST_CMDS + _HELP_CMDS

_BOT_NAME = 'Retrospective Bot'

_SLACK_RETRO_TOKEN = os.getenv('SLACK_RETRO_TOKEN')
_AIRTABLE_RETRO_BASE_ID = os.getenv('AIRTABLE_RETRO_BASE_ID')
_AIRTABLE_RETRO_API_KEY = os.getenv('AIRTABLE_RETRO_API_KEY')
_AIRTABLE_RETRO_ITEMS_TABLE_ID = 'Items'
_AIRTABLE_RETRO_ITEMS_CURRENT_VIEW = 'Current View'

_MISSING_ENV_VARIABLES = []
if not _SLACK_RETRO_TOKEN:
    _MISSING_ENV_VARIABLES.append('SLACK_RETRO_TOKEN')
if not _AIRTABLE_RETRO_BASE_ID:
    _MISSING_ENV_VARIABLES.append('AIRTABLE_RETRO_BASE_ID')
if not _AIRTABLE_RETRO_API_KEY:
    _MISSING_ENV_VARIABLES.append('AIRTABLE_RETRO_API_KEY')
if _MISSING_ENV_VARIABLES:
    _STEPS_TO_FINISH_SETUP = \
        'Need to setup the following AWS Lambda function env variables:\n{}'.format(
            _MISSING_ENV_VARIABLES)
else:
    _STEPS_TO_FINISH_SETUP = None
    _AIRTABLE_CLIENT = airtable.Airtable(
        _AIRTABLE_RETRO_BASE_ID, _AIRTABLE_RETRO_API_KEY)


@app.route('/')
def index():
    """Root endpoint."""
    if _STEPS_TO_FINISH_SETUP:
        status = '❗️{}'.format(_STEPS_TO_FINISH_SETUP)
    else:
        status = '✅'
    return '''Integration to store /retro Slack command in Airtable.<br>
        Status: {}<br>
        Link Slack webhook to post json to /handle_slack_notification'''.format(status), 200


@app.route('/handle_slack_notification', methods=['POST'])
def handle_slack_notification():
    """Receives a Slack webhook notification and handles it to update Airtable."""
    if _STEPS_TO_FINISH_SETUP:
        return _STEPS_TO_FINISH_SETUP, 200

    slack_notification = request.form
    # Verify that the request is authorized.
    if slack_notification['token'] != _SLACK_RETRO_TOKEN:
        abort(401)

    # Get the user name.
    user_name = slack_notification['user_name']

    # Get the slash command.
    slash_command = slack_notification['command']
    response_url = slack_notification['response_url']

    # Strip excess spaces from the text.
    full_text = slack_notification['text'].strip()
    full_text = re.sub(' +', ' ', full_text)
    command_text = full_text

    # The bot can be called in Slack with:
    if slash_command in _CATEGORY_CMDS:
        # '/good Bla Bla'
        command_action = slash_command
        command_params = command_text
    else:
        # '/retro good Bla Bla'
        command_action, command_params = _get_command_action_and_params(
            command_text)

    # If the command does not exist, show help.
    if command_action not in _ALL_CMDS:
        command_action = _HELP_CMDS[0]

    # Call different actions:
    # /retro good, /retro bad, /retro try
    if command_action in _CATEGORY_CMDS:
        category = command_action
        item_object = command_params
        response = _add_retrospective_item_and_get_response(
            category, item_object, user_name)
        return _format_json_response(response)

    # /retro list
    if command_action in _LIST_CMDS:
        response = _get_retrospective_items_response()
        return _format_json_response(response)

    # /retro new
    if command_action in _NEW_CMDS:
        item_object = command_params
        if item_object:
            response = 'Oops, did you mean "/retro good {}"?'.format(
                item_object)
        else:
            response = _mark_retrospective_items_as_reviewed(response_url)
        return _format_json_response(response)

    # /retro help
    if command_action in _HELP_CMDS or command_text == '' or command_text == ' ':
        response = '\n'.join([
            '*{command} good <item>* to save an item in the "good" list',
            '*{command} bad <item>* to save an item in the "bad" list',
            '*{command} try <item>* to save an item in the "try" list',
            '*{command} list* to see the different lists saved for the current sprint',
            '*{command} new* to start a fresh list for the new scrum sprint',
            '*{command} help* to see this message',
        ]).format(command=slash_command)
        # Don't show help to other users in th channel.
        return _format_json_response(response, in_channel=False)


def _get_command_action_and_params(command_text):
    """Parse the passed string for a command action and parameters."""
    command_components = command_text.split(' ')
    command_action = command_components[0].lower()
    command_params = ' '.join(command_components[1:])
    return command_action, command_params


def _add_retrospective_item_and_get_response(category, item_object, user_name):
    """Set the retrospective item for the passed parameters and return the approriate responses."""
    # Reject attempts to set reserved terms.
    if item_object.lower() in _ALL_CMDS:
        return "Sorry, but *{}* can't save *{}* because it's a reserved term.".format(
            _BOT_NAME, item_object)

    item_object = item_object.capitalize()
    category = category.lower()

    existing_item = _AIRTABLE_CLIENT.get(
        _AIRTABLE_RETRO_ITEMS_TABLE_ID,
        view=_AIRTABLE_RETRO_ITEMS_CURRENT_VIEW,
        filter_by_formula='AND(Category = "{}", Object = "{}")'.format(
            category, item_object.replace('"', '\"')),
    ).get('records')
    if existing_item:
        return 'This retrospective item has already been added!'

    item_airtable_record = _AIRTABLE_CLIENT.create(_AIRTABLE_RETRO_ITEMS_TABLE_ID, {
        'Category': category.lower(),
        'Object': item_object,
        'Creator': user_name,
        'Created At': datetime.utcnow().isoformat(timespec='milliseconds') + 'Z',
    })
    if not item_airtable_record:
        return 'Sorry, but *{}* was unable to save the retrospective item.'.format(_BOT_NAME)

    response = 'New retrospective item:'
    attachments = _get_retrospective_items_attachments([item_airtable_record])
    return (response, attachments)


def _get_retrospective_items_response():
    """Get all the retrospective item for the current sprint."""
    items = _AIRTABLE_CLIENT.get(
        _AIRTABLE_RETRO_ITEMS_TABLE_ID,
        view=_AIRTABLE_RETRO_ITEMS_CURRENT_VIEW,
    ).get('records')
    if not items:
        return 'No retrospective items yet.'

    response = 'Retrospective items:'
    attachments = _get_retrospective_items_attachments(items)
    return (response, attachments)


def _get_retrospective_items_attachments(retrospective_items):
    """Return Slack message attachements to show the given retrospective items."""
    retrospective_items = sorted(
        retrospective_items, key=lambda item: item['fields']['Category'])
    items_by_category = groupby(
        retrospective_items, lambda item: item['fields']['Category'])
    colors_by_category = {'good': 'good', 'bad': 'danger', 'try': 'warning'}
    attachments = [
        {
            'title': category.capitalize(),
            'text': '\n\n'.join(['• ' + item['fields']['Object'] for item in items_in_category]),
            'color': colors_by_category[category],
        }
        for category, items_in_category in items_by_category
    ]
    return attachments


def _mark_retrospective_items_as_reviewed(response_url):
    """Start a new sprint with a new empty retrospective item list."""
    _async_mark_retrospective_items_as_reviewed(response_url)
    return 'Marking all current retrospective items as reviewed...'


@task
def _async_mark_retrospective_items_as_reviewed(response_url):
    items = _AIRTABLE_CLIENT.get(
        _AIRTABLE_RETRO_ITEMS_TABLE_ID,
        view=_AIRTABLE_RETRO_ITEMS_CURRENT_VIEW,
    ).get('records')
    if not items:
        return requests.post(response_url, json={
            'response_type': 'in_channel',
            'text': 'All retrospective were already marked as reviewed!',
        })

    new_params = {
        'Reviewed At': datetime.utcnow().isoformat(timespec='milliseconds') + 'Z',
    }

    for item in items:
        _AIRTABLE_CLIENT.update(
            _AIRTABLE_RETRO_ITEMS_TABLE_ID, item.get('id'), new_params)

    remaining_items = _AIRTABLE_CLIENT.get(
        _AIRTABLE_RETRO_ITEMS_TABLE_ID,
        view=_AIRTABLE_RETRO_ITEMS_CURRENT_VIEW,
    ).get('records')
    attachments = _get_retrospective_items_attachments(remaining_items)

    return requests.post(response_url, json={
        'response_type': 'in_channel',
        'text': 'All retrospective items marked as reviewed!' +
        "\nHere are the remaining 'try' items to complete:" if attachments else '',
        'attachments': attachments,
    })


def _format_json_response(response, in_channel=True):
    """Format response for Slack."""
    if isinstance(response, str):
        text = response
        attachments = None
    else:
        text = response[0]
        attachments = response[1]

    response_dict = {
        'response_type': 'in_channel' if in_channel else 'ephemeral',
        'text': text,
        'attachments': attachments if attachments else []
    }

    response_json = json.dumps(response_dict)
    return Response(response_json, status=200, mimetype='application/json')


# We only need this for local development.
if __name__ == '__main__':
    app.run()
