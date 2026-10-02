"""Provider transport. Keys stay in the vault; errors never echo provider payloads."""
import json

import httpx


class ProviderError(ValueError):
    pass


class AIProvider:
    def __init__(self, connection, *, transport=None):
        self.connection = connection
        self.transport = transport

    async def request(self, path, payload):
        connection = self.connection
        headers = {'Authorization': 'Bearer ' + connection['api_key']} if connection.get('api_key') else {}
        try:
            async with httpx.AsyncClient(timeout=45, trust_env=False, transport=self.transport, follow_redirects=False) as client:
                response = await client.post(connection['base_url'] + path, headers=headers, json=payload)
                if response.status_code != 200:
                    raise ProviderError(f'AI provider returned HTTP {response.status_code}; check key, model access and quota')
                return response.json()
        except (httpx.HTTPError, json.JSONDecodeError):
            raise ProviderError('AI provider connection failed or returned invalid JSON') from None

    async def turn(self, instructions, history, tools):
        connection = self.connection
        common = {'model': connection['model'], 'parallel_tool_calls': False}
        if connection['protocol'] == 'responses':
            payload = {**common, 'store': False, 'instructions': instructions, 'input': history,
                       'max_output_tokens': 1500,
                       'tools': [{'type': 'function', **tool, 'strict': True} for tool in tools]}
            data = await self.request('/responses', payload)
            if data.get('status') != 'completed':
                raise ProviderError('AI response incomplete; no browser action was accepted')
            output = data.get('output', [])
            history.extend(output)
            calls = [item for item in output if item.get('type') == 'function_call']
            return [{'id': item['call_id'], 'name': item['name'], 'arguments': item['arguments']} for item in calls]
        functions = [{'type': 'function', 'function': {**tool, 'strict': True}} for tool in tools]
        data = await self.request('/chat/completions', {**common, 'messages': [{'role': 'system', 'content': instructions}, *history],
                                                       'tools': functions, 'max_tokens': 1500})
        choice = data.get('choices', [{}])[0]
        if choice.get('finish_reason') not in ('stop', 'tool_calls'):
            raise ProviderError('AI response incomplete; no browser action was accepted')
        message = choice['message']
        history.append(message)
        return [{'id': item['id'], **item['function']} for item in message.get('tool_calls', [])]

    def tool_result(self, history, call, result):
        content = json.dumps(result)
        if self.connection['protocol'] == 'responses':
            history.append({'type': 'function_call_output', 'call_id': call['id'], 'output': content})
        else:
            history.append({'role': 'tool', 'tool_call_id': call['id'], 'content': content})

    async def test(self):
        tool = {'name': 'connection_ok', 'description': 'Confirm the connection test.',
                'parameters': {'type': 'object', 'properties': {}, 'required': [], 'additionalProperties': False}}
        calls = await self.turn('Call connection_ok once. This is a connection test.',
                                [{'role': 'user', 'content': 'Test tool calling.'}], [tool])
        if len(calls) != 1 or calls[0]['name'] != 'connection_ok' or json.loads(calls[0]['arguments']) != {}:
            raise ProviderError('The model did not pass the tool-calling test')
        return {'ok': True, 'message': 'API key, model and tool calling verified'}
