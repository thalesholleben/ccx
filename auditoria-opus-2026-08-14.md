# Auditoria final — CCX

Data: 2026-08-14  
Revisor independente: Claude Code, chamado diretamente com `--model opus --effort high`  
Modo: somente leitura; sem edição, login, troca de conta, tarefa agendada ou chamada de rede.

## Veredito

**APROVADO_COM_RESSALVAS** — nenhum bloqueador encontrado.

## Escopo auditado

- #21: launcher opt-in `ccx_profile.py` com `CLAUDE_CONFIG_DIR`/`CODEX_HOME` por processo;
- #23: identidade de processo para locks internos, além de PID;
- #154: desinstalação segura do monitor;
- #155: continuidade de `ccx_codex auto` após exceção inesperada;
- #156: proteção contra remoção de `owner.json` em retomada concorrente;
- #157: preflight do Python e diagnóstico seguro de falha precoce;
- documentação, testes e regras de segurança correspondentes.

## Evidências executadas pelo revisor

| Verificação | Resultado |
| --- | --- |
| `git status --porcelain` e `git diff` | working tree revisado, incluindo arquivos não rastreados |
| `python -m py_compile` nos arquivos Python | passou (Python 3.12.10) |
| `python test_ccx.py` | 42/42 passaram |
| `python test_ccx_codex.py` | 19/19 passaram |
| `python test_ccx_profile.py` | 4/4 passaram |
| Contagens em `README.md` e `AGENTS.md` | conferem com as suítes |
| Parser PowerShell sem execução do instalador | passou |
| Varredura do diff por padrões de segredo | nenhum segredo introduzido |

Também foram verificadas, fora da lista fornecida pelo autor:

1. Compatibilidade do lock externo do Claude Code: ele continua vazio; somente locks internos recebem `owner.json`.
2. Estabilidade real da marca de criação de processo no Windows: mesma marca para o mesmo PID, marca divergente para processo distinto, fallback correto para lock legado e para marcador indisponível.
3. Todos os pontos de chamada de `discard_lock` contra a nova semântica sem `owner_id`.
4. Ordem dos `except` no monitor Claude/Codex: `TimeoutError` permanece específico e `KeyboardInterrupt`/`SystemExit` não são engolidos.

## Achados do revisor

### R1 — Baixa — credenciais de ambiente herdadas por `ccx_profile.py`

Se `ANTHROPIC_API_KEY`, `CLAUDE_CODE_OAUTH_TOKEN` ou `OPENAI_API_KEY` estiverem no shell pai, dois perfis podem autenticar pela mesma variável de ambiente. Não há cópia ou vazamento de segredo: é herança deliberada do ambiente filho. O contrato atual do launcher é preservar o ambiente e trocar apenas o diretório de configuração; por isso esta observação não altera a task #21 nem bloqueia o uso OAuth por perfil.

### R2 — Baixa — permissões padrão do diretório de perfil no Linux

O diretório é criado com a permissão padrão do `umask`; os clientes continuam responsáveis por criar `auth.json` com sua própria proteção. Não é regressão do CCX e a plataforma operacional deste monitor é Windows. Sem alteração neste lote.

### R3 — Baixa — paridade de diagnóstico pré-loop no Codex

`ccx_codex auto` agora sobrevive a erro dentro do loop, que é o escopo da #155. Ele não ganhou o log de falha anterior ao loop do monitor Claude. A regra em `AGENTS.md` é específica de `ccx auto`; não há pendência funcional da #155.

### R4 — Informativa — teste PowerShell é estático

O instalador foi validado por parser, preflight real e asserções de contrato no arquivo. O revisor não recomenda introduzir Pester para este repositório stdlib pequeno.

### R5 — Informativa — macOS e perfis Claude

O README já declara a limitação do Keychain no macOS. O launcher é documentado como isolamento de diretório/processo, não como migração de credencial de processo persistente.

### R6 — Informativa — owner interno malformado sem `id`

O caso exige um `owner.json` impossível de ser produzido pelo protocolo atual, pois a escrita é atômica e sempre inclui `pid` e `id`. Não é regressão e não justifica aumentar o protocolo de lock.

## Análise e decisão

As seis ressalvas são não bloqueantes e não correspondem a uma pendência aberta do CCX. Depois da revisão, houve somente um ajuste de precisão em `AGENTS.md`: a regra de diagnóstico pré-loop foi nomeada explicitamente como `ccx.py auto`, removendo a ambiguidade apontada em R3. Nenhum comportamento de produção foi alterado após o parecer.

As tasks #23, #155, #156 e #157 podem ser concluídas. As tasks #21 e #154 já estavam concluídas. A validação operacional ponta a ponta do instalador/desinstalador não foi executada para não interromper o monitor apenas durante a auditoria; o código foi validado por parser, preflight real e revisão independente.

## Verificação operacional posterior

Após o parecer, com a janela de produção liberada pelo responsável, o monitor foi
atualizado de forma controlada:

1. `install-ccx-monitor.ps1 -Uninstall` desabilitou/desregistrou a tarefa e encerrou
   o `pythonw ccx.py auto` previamente verificado pela imagem e linha de comando.
2. `install-ccx-monitor.ps1` recriou e iniciou o watchdog com o Python 3.12 validado.
3. A nova instância publicou `owner.json` com `process_start`; a tarefa retornou ao
   estado `Ready` com `LastTaskResult = 0`, e o processo confirmado é o `ccx.py auto`
   deste diretório.

## Conclusão

Os sete itens do lote foram considerados consistentes com os critérios de aceite. Não há bloqueador de segurança, concorrência, credenciais ou operação para impedir o encerramento das pendências atuais do CCX.
