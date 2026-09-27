> Historical compatibility reference. Global rotation/bridge entry points are retired.
> Use [fleet mode](../features/fleet.md) and the [migration guide](../runbooks/migration.md).

# Contrato local Claude, versão 1

Entrada implementada: `python ccx.py status --json`. Saída: um objeto JSON em
stdout, exit 0. Falha de snapshot: exit 4, stdout vazio e um objeto sanitizado em
stderr com `schema_version=1`, `provider=claude` e `error_code` igual a
`store_corrupt`, `store_busy`, `invalid_state` ou `snapshot_unavailable`.
Sem traceback, caminhos, payload ou mensagem bruta de exceção. Erro de argumentos
continua sendo exit 2 do argparse, fora do contrato de snapshot. Não substituir
falha por snapshot vazio. Não há HTTP, servidor, Electron ou event bus.

Este comando lê arquivos sob o lock do store, sem chamar usage, autenticar,
renovar token ou gravar contas. O lock cria/remove seu próprio arquivo de
coordenação. O host privilegiado pode executá-lo e entregar a projeção à UI;
o renderer não deve ler o store OAuth.

## Campos

| Campo | Semântica |
|---|---|
| `schema_version` | Inteiro `1`; consumidor recusa major desconhecido |
| `provider` | `claude` |
| `generated_at` | Hora UTC de geração, não hora da medição |
| `control_generation` | Revisão de pin/cadastro/troca no código novo; legado começa em 0 |
| `selected_slot`, `pinned_slot` | Seleção global e trava do operador, não identidade de todos os processos |
| `slots[].auth_state` | `invalid`, `revoked`, `expired`, `refresh_required` ou `unverified`; sempre avaliação local |
| `slots[].quota.snapshot_at`, `age_seconds` | Timestamp/idade do cache, não medição comprovada |
| `slots[].quota.measurement_at`, `measurement_verified` | `null` e `false`: o cache legado não preserva procedência suficiente |
| `slots[].quota.freshness` | `recent_snapshot`, `stale_snapshot`, `unknown` |
| `slots[].quota.availability_confirmed` | `false`: não existem leases controlando os consumidores |
| `slots[].quota.windows` | Somente `5h`, `7d`, `modelo` com percentuais finitos de 0 a 100 |
| `windows.*.percent`, `snapshot_percent` | Valor projetado para exibição e valor encontrado no cache |
| `windows.*.source`, `reset_at` | `projected` após reset vencido, senão `legacy_cache`; reset UTC quando conhecido |
| `slots[].quota.error_code` | Enum sanitizado; `measurement_rate_limited` não significa limite do modelo |
| `monitor.alive` | Processo do rotador legado detectado, não saúde de jobs |
| `monitor.job_progress_verified` | `false` |
| `execution` | `mode=legacy`, `automatic_recovery_available=false` |

Não há tokens, e-mails, UUID de organização, prompts, transcripts ou payloads
de ferramentas. Campos arbitrários do cache não são serializados. A leitura
não corrige registros malformados e não migra stores.

`refresh_required` significa acesso expirado (incluindo a margem de 5 minutos)
com string de refresh presente, não refresh validado. Só o rotador legado aceita
publicar esse grant para o CLI renovar, preservando seu comportamento anterior.
Não usar essa classificação como permissão de admissão do futuro supervisor.
Nenhuma recuperação é confirmada até prova real da identidade/progresso.

O coletor legado zera percentuais projetados no próprio cache e pode renovar
`known_at` num fallback de 429. Portanto nem seu percentual nem seu timestamp
podem ser promovidos a uma medição real. Uma UI deve identificar "cache" ou
"projeção" e não desenhar disponibilidade verde confirmada a partir desses dados.

## Observação de uma tentativa

`ccx_runtime.AttemptObservation(session_id, attempt)` consome objetos decodificados
do `stream-json --verbose` do Claude, sem `--include-partial-messages`.
`finish(exit_code, checkpoint_verified=False, cancelled=False)` devolve uma
`AttemptOutcome` com `schema_version`, `state`, `reason_code`, `action`,
`session_id` e `attempt`. O chamador precisa demultiplexar tentativas: nunca
reutilizar um observador para outro filho, mesmo com o mesmo session_id.

O parser não abre pipes, mata processos, confirma persistência de transcript,
prova efeito externo ou implementa retry. `checkpoint_verified=True` é uma
obrigação do futuro supervisor, não uma conclusão obtida pelo parser.

| Evidência | Resultado possível |
|---|---|
| Exit 0, terminal completed, is_error false e nenhum erro/ferramenta pendente | completed |
| authentication_failed com terminal de erro e checkpoint comprovado | waiting_auth / reauthenticate |
| rate_limit tipado com terminal de erro e checkpoint comprovado | recovering / select_account, sugestão, não troca |
| overloaded/server_error nas mesmas condições | recovering / retry_account |
| invalid_request, billing, conta suspensa, modelo ou erro desconhecido | needs_attention / stop |
| Cancelamento | cancelled, nenhuma tentativa nova |
| Sessão divergente, terminal ausente/duplicado ou ferramentas ambíguas | needs_attention / stop |

Não existe promessa de exatamente uma execução de comandos externos. Eventos
não comprovam que uma ferramenta concluída teve seu resultado salvo. Retomar
um trabalho continua proibido quando esse checkpoint é desconhecido.

## Validação

`python test_ccx_runtime.py` roda somente contra temporários e credenciais
sintéticas, com rede proibida. O canário oficial separado demonstrou resume
entre contas e erro 401 em 2.1.238/2.1.263, sem ferramentas. Refresh único,
ferramentas reais, VS Code e supervisor continuam gates independentes abertos.
