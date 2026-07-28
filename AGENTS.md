# AGENTS.md

Mapa rapido para quem (humano ou agente) for mexer neste repositorio.

## O que e

Dois scripts Python stdlib independentes, cada um monitorando a cota de um
provedor e trocando de conta antes de bater o limite:

- `ccx.py` - contas Claude Code (le `~/.claude/.credentials.json` e `~/.claude.json`)
- `ccx_codex.py` - contas Codex CLI / ChatGPT (le `~/.codex/auth.json`), importa
  `ccx.py` e reusa de la a engine de decisao e os primitivos de IO/lock

Sem framework, sem dependencia externa, sem servidor. So leitura de cota e
troca cirurgica de credencial nos arquivos que o proprio Claude Code / Codex
CLI ja usam.

## Comandos de validacao

```bash
python test_ccx.py         # 15 testes, engine de decisao + IO do modulo Claude
python test_ccx_codex.py   # 12 testes, especifico do modulo Codex
```

Sem framework de teste, so `assert` e um runner minimo no final de cada
arquivo. Rodar os dois antes de qualquer PR.

## Regras ao editar

- Qualquer troca de credencial tem que continuar **cirurgica**: reescrever so
  os campos que identificam a conta, nunca o arquivo inteiro. O
  `.credentials.json` do Claude Code guarda `mcpOAuth` (tokens de servidores
  MCP) que uma escrita completa derrubaria.
- Identidade de conta ativa casa por **email/organizacao**, nunca por token.
  Os dois provedores rotacionam o refresh token da conta em uso; comparar por
  token faz a identidade se perder na primeira rotacao.
- Toda escrita em disco e via arquivo temporario + `os.replace` (atomica).
  JSON corrompido levanta `CorruptFile`, nunca vira `{}` silenciosamente (um
  `{}` faria o passo seguinte reescrever o arquivo e apagar o que nao foi
  lido).
- `ccx_codex.py` importa `ccx.py` para reusar a engine de decisao
  (`pick_target`, `band_delay`, `next_wake`) e os primitivos de lock/IO. Nao
  duplicar essas funcoes; se algo generico precisar mudar, muda em `ccx.py` e
  o modulo Codex herda.
- Nunca commitar `~/.ccx/accounts.json` ou `~/.ccx/codex_accounts.json`
  (tokens OAuth reais). Ja estao no `.gitignore`.

## O que ignorar

- `__pycache__/`, `.pytest_cache/`
