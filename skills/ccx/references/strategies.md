# Estratégias de despacho

| Situação | Decisão |
|---|---|
| Tarefa comum, sem conta especificada | Provedor/modelo exigidos, seleção automática de célula |
| Pedido explícito para conta X | `--cell X`; indisponível significa aguardar/diagnosticar, não trocar escondido |
| Tarefa longa ou cara | Custo conservador, folga de sessão e semanal, perfis livres; não usar só o percentual |
| Várias alterações no mesmo repo | Worktrees independentes preparadas antes do despacho |
| Várias leituras no mesmo repo | Podem coexistir se há perfis livres e capacidade |
| Conta com 401/403 ou sem dados atuais | Login/diagnóstico; não interpretar como cota disponível |
| Job falhou após possível efeito | Inspecionar resultado e estado; não repetir automaticamente |
| Cliente de espera deu 124 | Job pode estar executando; consultar ou cancelar e confirmar antes de repetir |
| Nenhuma célula autenticada | Informar dependência; manter executor legado autorizado fora da frota |

Exemplo: com 50% de uso e margem 10%, Pro tem folga de sessão 0,40x, Max 5 tem
2,00x e Max 20 tem 8,00x. Semanal e limites do modelo ainda podem eliminar qualquer
uma delas. Reset próximo desempata candidatas, não substitui a verificação de cota.

Separar conta para tarefas pesadas é uma preferência por capacidade, não regra
fixa de que toda revisão vai para Max 20. Modelo/esforço e requisitos da tarefa
continuam os mesmos em qualquer conta. Não trocar por um modelo mais barato para
fazer a tarefa caber sem autorização.

## Migração gradual

Frota instalada não migra automaticamente VS Code, chats, monitores, rotinas ou
cross-review. Cadastre e autentique, valide dois jobs benignos em contas distintas
e então adapte cada executor para enviar jobs pelo CLI. Só retire a rotação legada
do fluxo depois de confirmar sua migração. Não matar sessões alheias para trocar conta.

O painel serve para cadastro e acompanhamento; o despacho vem dos agentes e das
integrações. A skill não agenda rotinas, não altera runners oficiais e não instala
autostart. Cada ação precisa estar no escopo autorizado pelo usuário.
