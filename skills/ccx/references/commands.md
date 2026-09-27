# Comandos do CCX

Execute na pasta informada por `installation.json`, ou use o caminho absoluto do
CLI. `--root` é opção da frota e vem antes do subcomando. Estado padrão: `~/.ccx/fleet`.

```powershell
python ccx-fleet.py status --json
python ccx-fleet.py status --no-color
python ccx-fleet.py service start
python ccx-fleet.py service status
python ccx-fleet.py service stop

python ccx-fleet.py cell add conta-x claude --plan max20
python ccx-fleet.py cell login conta-x
python ccx-fleet.py worker add conta-x
python ccx-fleet.py cell login conta-x --worker ID_DO_WORKER
python ccx-fleet.py cell configure conta-x --plan max5
python ccx-fleet.py cell pause conta-x
python ccx-fleet.py cell resume conta-x
```

Cadastro no painel: nome, provedor e plano. Claude: pro/max5/max20/custom; Codex:
pro/custom. Não inferir plano a partir de usage 0%. Os nomes de plano do CCX são
perfis de capacidade, não uma consulta automática à assinatura. O CLI conserva
ajuste avançado de peso, peso semanal e margem; não alterar sem necessidade e
evidência. Não existe mais `--max-active`.

```powershell
python ccx-fleet.py submit claude --cell conta-x --prompt-file tarefa.md --cwd C:\work\app --title "Revisar implementação" --cost 15 --request-id revisao-123
python ccx-fleet.py run codex --prompt-file tarefa.md --cwd C:\work\app --permission write --effort high --request-id implementacao-123 --timeout 900
python ccx-fleet.py wait ID_DA_TAREFA --timeout 900
python ccx-fleet.py cancel ID_DA_TAREFA
```

- `--model`: identificador compatível com o CLI instalado; preserve escolha explícita.
- `--effort`: Claude low/medium/high/max; Codex low/medium/high/xhigh.
- `--cost`: estimativa de 0,1 a 80 pontos equivalentes x1, padrão 15.
- `--priority`: 0 a 10; maior primeiro, sem furar regras de cota/perfil/pasta.
- `--cell`: somente aquela conta, sem fallback silencioso.
- `--request-id`: deduplicação; reapresentação com conteúdo diferente é recusada.
- Sem `--prompt-file`, lê stdin. Não passe prompt privado na linha de comando.

Saída de `submit` identifica o job. `run` imprime o ID em stderr e espera o resultado.
`wait`: 0 concluída, 1 falha/cancelada/atenção, 3 serviço morto com tarefa na fila,
124 tempo de espera encerrado com tarefa preservada. Erros de comando/validação
retornam código não zero e mensagem sanitizada. `cancel` é solicitação; confirme
estado final e liberação do worker antes de reexecutar o trabalho.

Resultados privados: `~/.ccx/fleet/jobs/<id>/result.json`. O JSON de status não
contém prompt ou token. A UI acompanha tarefas enviadas por integrações; não tem
formulário para criá-las. Abrir painel: `ccx-panel.cmd` (Windows).

## Legado

`ccx.py stats`, `ccx.py switch` e `ccx_codex.py switch` pertencem ao monitor
antigo. Não operam células. Consulte o runbook de migração do repositório se
precisar desativar hooks, monitores ou bridge antigos. Não os reative para
despachar pela frota, nem remova o registro de um bridge vivo.

Esforços listados aqui são os aceitos pelo CCX. Se o pedido usar outro nível,
informe que ainda não é suportado pela frota; não reduza o esforço silenciosamente.
