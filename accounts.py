"""Provider processes use account sign-in, with API billing variables removed."""
import os


def account_environment(environment=None):
    env = dict(os.environ if environment is None else environment)
    for key in ['OPENAI_API_KEY', 'CODEX_API_KEY', 'OPENAI_BASE_URL', 'OPENAI_API_BASE',
                'ANTHROPIC_API_KEY', 'ANTHROPIC_AUTH_TOKEN', 'ANTHROPIC_BASE_URL',
                'CLAUDE_CODE_USE_BEDROCK', 'CLAUDE_CODE_USE_VERTEX', 'CLAUDE_CODE_USE_FOUNDRY']:
        env.pop(key, None)
    env['DISABLE_AUTOUPDATER'] = '1'
    return env
