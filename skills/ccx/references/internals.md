# Contrato interno e limites

Estado SQLite em `~/.ccx/fleet/fleet.sqlite3`; perfis em
`profiles/<provider>/<worker>`. O cadastro cria um worker vazio. Login é feito
pelo CLI oficial na home exclusiva. Acrescentar workers exige logins independentes
e validação de coexistência; nunca clonar o grant de um perfil já autenticado.

O scheduler, sob uma transação, verifica:

1. Serviço admitindo, célula não pausada, identidade vinculada e provedor/modelo compatível.
2. Perfil autenticado, ocioso, sem login/renovação concorrente e fora de backoff.
3. Medição conhecida, idade entre 0 e 600s e nenhum reset aplicável expirado.
4. Folga após uso, tendência de consumo, margem e todas as reservas aplicáveis.
5. Ausência de escrita concorrente na mesma pasta ou em ancestral/descendente.

Fórmula por janela: custo reservado = custo x1 / peso da janela. Folga antes da
nova reserva = 100 - uso projetado - margem - reservas existentes. Entre candidatas,
escolhe menor pressão da reserva sobre a folga; desempata por reset e último despacho.
O multiplicador de sessão não é aplicado automaticamente ao semanal/modelo.

Semanal padrão 1 + custo 15 + margem 10 significa que uso semanal acima de 75%
impede esse despacho mesmo em Max 20. Ajuste de custo/pesos exige evidência, não
serve para forçar passagem por uma cota sem capacidade. Reservas são estimativas,
não garantia de tokens ou previsão exata do provedor.

Reservas de jobs concluídos ficam retidas por ao menos 60s e até uma medição nova
iniciada depois desse prazo. Reservas ativas continuam inteiras, mesmo se parte do
consumo já apareceu no uso. A política é deliberadamente conservadora.

Coleta: a cada 180s em conta ativa, 240s em conta ociosa com fila compatível/erro.
Sem fila ou tarefas ativas, mede contas autenticadas e não pausadas a cada 900s,
para refletir consumo externo à frota. O serviço deve estar ligado; painel fechado
não interrompe a coleta. Backoff e falhas do provedor podem atrasar a atualização.
A tela pode mostrar medição vencida; isso não significa conta vazia nem autorização para
admitir. Login faz a medição inicial. Reset nulo com uso conhecido permanece
desconhecido e sujeito ao TTL. HTTP 429 não vira uso 0%.

Refresh só em worker ocioso com lock exclusivo. Durante execução, o CLI é dono
do perfil e o CCX não concorre por seu refresh. Revogação/auth inválida bloqueiam
o perfil; erro transitório aplica backoff de 240s. Não há retry automático do prompt.

Runner reivindica o ticket uma vez. Só há sucesso com terminal válido, exit 0,
resultado não vazio e nenhum erro informado. Processo perdido ou efeito ambíguo
fica `needs_attention`. Cancelamento encerra somente a árvore daquele runner.

Sem teto numérico de agentes: quantidade depende de perfis livres, cota e recursos
da máquina. Schema 3 preserva colunas antigas max_active=0, sem usá-las como teto.
Não existe resume automático entre contas, nem migração de chats já abertos.

Windows é a plataforma exercitada para serviço, Job Object, bootstrap e bandeja.
Fechar UI/terminal não encerra jobs; reboot ou logout não têm continuidade garantida.
MCP/settings/hooks pessoais não são copiados. Ambiente herdado é restrito, e guardas
de recursão pertencem à tarefa. Configure integrações do perfil enquanto ocioso.
