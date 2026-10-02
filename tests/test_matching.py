from fastapi.testclient import TestClient

from retail.app import create_app

HEADERS = {'X-Retail-Client': 'dashboard'}


def setup_records(client):
    account = client.post('/api/accounts', json={'name': 'Account', 'email': 'person@example.com'}).json()
    profile = client.post('/api/profiles', json={'name': 'Profile', 'email': ' Person@EXAMPLE.com '}).json()
    group = client.post('/api/groups', json={'name': 'Group', 'products': 'B012345678;25'}).json()
    config = {'group_id': group['id'], 'profile_id': profile['id'], 'match_profiles': True,
              'simulation': True, 'count': 1}
    return account, profile, config


def test_email_match_manual_override_and_stale_preview(tmp_path):
    with TestClient(create_app(tmp_path), headers=HEADERS) as client:
        account, profile, config = setup_records(client)
        # Hidden manual fields must not influence automatic assignment.
        preview = client.post('/api/assignments/preview', json={**config, 'account_group_id': 'deleted'}).json()
        assert not preview['errors']
        assert preview['rows'][0]['account_id'] == account['id']
        assert preview['rows'][0]['match_source'] == 'email'
        assert client.get('/api/state').json()['account_profiles'] == []
        other = client.post('/api/accounts', json={'name': 'Other', 'email': 'other@example.com'}).json()
        client.post('/api/organization/relationship', json={'account_id': other['id'], 'profile_id': profile['id']})
        assert client.post('/api/assignments/create', json={**config, 'preview': preview['rows']}).status_code == 422
        manual_link = client.post('/api/assignments/preview', json=config).json()
        assert manual_link['rows'][0]['account_id'] == other['id']
        assert manual_link['rows'][0]['match_source'] == 'manual_link'
        created = client.post('/api/assignments/create', json={**config, 'preview': manual_link['rows']})
        assert created.status_code == 200
        assert created.json()['created'][0]['account_id'] == other['id']
        manual = client.post('/api/assignments/preview', json={**config, 'match_profiles': False, 'account_id': account['id']}).json()
        assert manual['rows'][0]['account_id'] == account['id']
        assert manual['rows'][0]['match_source'] == 'selection'
        client.post('/api/organization/relationship', json={'account_id': other['id'], 'profile_id': profile['id'], 'enabled': False})
        assert client.post('/api/assignments/preview', json=config).json()['rows'] == preview['rows']


def test_ambiguity_retailer_scope_and_relationship_conflicts(tmp_path):
    with TestClient(create_app(tmp_path), headers=HEADERS) as client:
        account, profile, config = setup_records(client)
        other_site = client.post('/api/accounts', json={'name': 'Other site', 'email': 'person@example.com', 'retailer': 'walmart'}).json()
        client.post('/api/organization/relationship', json={'account_id': other_site['id'], 'profile_id': profile['id']})
        preview = client.post('/api/assignments/preview', json=config).json()
        assert preview['rows'][0]['account_id'] == account['id']
        duplicate = client.post('/api/accounts', json={'name': 'Duplicate', 'email': 'person@example.com'}).json()
        ambiguous = client.post('/api/assignments/preview', json=config).json()
        assert ambiguous['errors'] and ambiguous['rows'][0]['account_id'] == ''
        assert client.post('/api/assignments/create', json={**config, 'preview': preview['rows']}).status_code == 422
        client.post('/api/organization/relationship', json={'account_id': account['id'], 'profile_id': profile['id']})
        assert not client.post('/api/assignments/preview', json=config).json()['errors']
        client.post('/api/organization/relationship', json={'account_id': duplicate['id'], 'profile_id': profile['id']})
        assert client.post('/api/assignments/preview', json=config).json()['errors']


def test_no_guessing_from_aliases_names_or_missing_information(tmp_path):
    with TestClient(create_app(tmp_path), headers=HEADERS) as client:
        account, profile, config = setup_records(client)
        for email in ['', 'person', 'per.son@example.com', 'person+family@example.com', 'someone@example.com']:
            profile_data = {**profile, 'email': email, 'name': account['name']}
            response = client.put('/api/profiles/' + profile['id'], json=profile_data)
            assert response.status_code == 200
            preview = client.post('/api/assignments/preview', json=config).json()
            assert preview['errors'] and preview['rows'][0]['match_source'] == 'unresolved'
        missing_profile = client.post('/api/assignments/preview', json={**config, 'profile_id': ''}).json()
        assert 'Select a profile' in missing_profile['errors'][0]
