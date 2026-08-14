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
python test_ccx.py         # 42 testes, engine de decisao + IO do modulo Claude
python test_ccx_codex.py   # 30 testes, especifico do modulo Codex
python test_ccx_profile.py # launcher opt-in de perfis isolados
```

Sem framework de teste, so `assert` e um runner minimo no final de cada
arquivo. Rodar os três antes de qualquer PR.

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
- `status`, `hook` e `auto` compartilham `usage_cache`. Qualquer coleta deve
  adquirir o lock e reler o store antes de decidir o que consultar: processos
  concorrentes podem ter preenchido o cache enquanto este processo esperava.
- `HTTP 429` na leitura de uso e falha de medicao, nao prova de cota esgotada.
  Sem leitura conhecida, manter a ativa. Se uma leitura recente ja a confirmou
  esgotada, ela pode orientar a troca mesmo que a releitura tenha dado 429.
- `do_switch` deve reler o store depois de adquirir o lock. A coleta solta o
  lock antes da decisao, e gravar o snapshot antigo pode ressuscitar refresh
  token ou apagar cache que outro hook acabou de salvar.
- `pinned_slot` e uma trava de operador, nao uma preferencia. Quando existe,
  `check_once` sai antes de qualquer coleta: fixar uma conta nao pode gerar
  trafego de usage. E o `do_switch(..., only_if_pinned=True)` revalida a
  fixacao ja sob o lock, senao um `--pin off` concorrente seria desfeito por
  uma troca decidida antes dele.
- `pinned_slot` invalido (slot inexistente ou tipo errado) levanta erro em vez
  de virar `None`. Cair em rotacao silenciosa e pior do que parar: o operador
  fixou justamente para a conta nao mudar.
- Locks internos do CCX guardam PID, id e marca de criação do processo. PID vivo
  com marca divergente é reciclado e pode ser retomado; marca temporariamente
  ilegível ou lock legado sem marca conserva o lock enquanto o PID estiver vivo.
  `discard_lock(..., None)` só pode remover um diretório ainda vazio: nunca apague
  `owner.json` sem conferir a identidade do dono.
- Falha antes do loop de `ccx.py auto` só vai para `auto.log` como classe da exceção
  ou como configuração insuficiente; nunca registre `str(exc)`, token ou payload.
- A troca do arquivo global de autenticacao serve para uso sequencial. Um
  processo persistente do Codex pode manter a identidade em memoria e nao
  migrar de conta. Nao documentar esse hot-swap como isolamento seguro entre
  agentes paralelos; isso exige perfis por processo (`CODEX_HOME` /
  `CLAUDE_CONFIG_DIR`) ou um proxy com afinidade de sessao.
- `ccx_profile.py` e o caminho opt-in para paralelismo: não copie tokens nem
  mude o ambiente do processo pai. Ele só cria o diretório do perfil e inicia
  um processo filho com `CLAUDE_CONFIG_DIR` ou `CODEX_HOME` próprio. Para
  Codex, preserve a flag `cli_auth_credentials_store="file"` antes dos args.
- Nunca commitar `~/.ccx/accounts.json` ou `~/.ccx/codex_accounts.json`
  (tokens OAuth reais). Ja estao no `.gitignore`.

## O que ignorar

- `__pycache__/`, `.pytest_cache/`
