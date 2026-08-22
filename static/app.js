// HTTP status code explanations for common/proxy-relevant codes
const HTTP_STATUS_EXPLANATIONS = {
    400: 'Bad Request — the request was malformed',
    401: 'Unauthorized — invalid or missing API key',
    403: 'Forbidden — access denied',
    404: 'Not Found — the requested resource is not available',
    408: 'Request Timeout — the server timed out waiting for the request',
    413: 'Payload Too Large — request exceeds the maximum allowed size',
    429: 'Rate Limited — too many requests, please slow down',
    500: 'Internal Server Error — the upstream server encountered an error',
    502: 'Bad Gateway — the upstream server returned an invalid response',
    503: 'Service Unavailable — the service is temporarily unavailable',
    504: 'Gateway Timeout — the upstream server timed out',
    520: 'Cloudflare 520 — origin server returned an empty or unknown response',
    521: 'Cloudflare 521 — the origin server is down or refused the connection',
    522: 'Cloudflare 522 — connection to the origin server timed out',
    523: 'Cloudflare 523 — the origin server is unreachable',
    524: 'Cloudflare 524 — a connection timeout occurred (origin too slow)',
    525: 'Cloudflare 525 — SSL handshake failed between Cloudflare and the origin',
    526: 'Cloudflare 526 — invalid SSL certificate on the origin server',
    530: 'Cloudflare 530 — origin DNS error (domain does not resolve)',
};

// ── i18n ──

const LOCALE = {
    en: {
        'nav.stats': 'Token Stats',
        'nav.logs': 'Logs',
        'nav.quotas': 'Quotas',
        'nav.free_models': 'Free Models',
        'nav.config': 'Configuration',
        'free_models.title': 'Free Models',
        'free_models.hint': 'Manage access to free model endpoints',
        'free_models.enable': 'Enable free models',
        'free_models.proxy_mode': 'Proxy mode',
        'free_models.mode_vpn': 'VPN (OpenVPN)',
        'free_models.mode_socks5': 'SOCKS5',
        'free_models.mode_direct': 'Direct',
        'free_models.socks5_title': 'SOCKS5 Proxies',
        'free_models.socks5_hint': 'Proxy list for routing free model requests',
        'free_models.socks5_add': 'Add proxy',
        'free_models.socks5_test': 'Test',
        'free_models.socks5_host': 'Host',
        'free_models.socks5_port': 'Port',
        'free_models.socks5_user': 'Username (optional)',
        'free_models.socks5_pass': 'Password (optional)',
        'free_models.socks5_testing': 'Testing...',
        'free_models.socks5_ok': 'Connection OK',
        'free_models.socks5_fail': 'Failed',
        'free_models.socks5_remove': 'Remove this proxy?',
        'free_models.socks5_rotation': 'Auto rotation between proxies',
        'free_models.socks5_rotate_on': 'Rotation enabled',
        'free_models.socks5_rotate_off': 'Rotation disabled',
        'vpn.title': 'VPN & IP Rotation',
        'vpn.hint': 'Rotate IP addresses for free model quota via OpenVPN',
        'vpn.status': 'Status',
        'vpn.current_ip': 'Current IP',
        'vpn.current_server': 'Server',
        'vpn.requests_this_ip': 'Requests this IP',
        'vpn.total_free': 'Total free requests',
        'vpn.enable': 'Enable VPN rotation',
        'vpn.enabled': 'VPN rotation enabled',
        'vpn.disabled': 'VPN rotation disabled',
        'vpn.connect': 'Connect',
        'vpn.switch_ip': 'Switch IP',
        'vpn.disconnect': 'Disconnect',
        'vpn.servers': 'VPN Servers',
        'vpn.add_server': 'Add Server',
        'vpn.ip_history': 'IP History',
        'vpn.credentials': 'NordVPN Credentials',
        'vpn.credentials_hint': 'Service credentials from NordVPN dashboard (Manual Setup)',
        'vpn.save_credentials': 'Save',
        'vpn.browse': 'Browse',
        'vpn.credentials_saved': 'Credentials saved',
        'vpn.credentials_missing': 'No credentials saved',
        'vpn.connected': 'Connected',
        'vpn.connecting': 'Connecting...',
        'vpn.disconnected': 'Disconnected',
        'vpn.error': 'Error',
        'vpn.rotation': 'Rotation',
        'vpn.rotation_on': 'Enabled',
        'vpn.rotation_off': 'Disabled',
        'vpn.ips_used': 'IPs used',
        'vpn.switches': 'Switches',
        'vpn.servers_hint': 'NordVPN .ovpn configs',
        'vpn.confirm_remove': 'Remove this server?',
        'vpn.server_added': 'Server added',
        'vpn.api_discovery': 'NordVPN API Discovery',
        'vpn.api_hint': 'Auto-discover servers via NordVPN API',
        'vpn.advanced': 'Advanced Settings',
        'vpn.health_check': 'Health Check',
        'vpn.config': 'VPN Configuration',
        'vpn.mode': 'Mode',
        'vpn.proxy_port': 'Proxy port',
        'vpn.switch_delay': 'Switch delay (s)',
        'vpn.docker_image': 'Docker image',
        'vpn.config_summary': 'Config saved automatically',
        'vpn.free_map': 'Free Model Map',
        'vpn.free_map_desc': 'Map each paid model to its free equivalent (hot-reload, no restart).',
        'vpn.rotation_rules': 'Rotation Rules',
        'vpn.rotation_rules_desc': 'Model-based rotation rules (hot-reload).',
        'vpn.schedule': 'Schedule',
        'vpn.schedule_desc': 'Cron-based rotation schedule (hot-reload).',
        'vpn.schedule_enabled': 'Enabled',
        'config.web_port': 'Web UI Port',
        'last.update': 'Last update: ',
        'stats.overview': 'Overview',
        'stats.input': 'Input',
        'stats.output': 'Output',
        'stats.cache': 'Cache',
        'stats.total': 'Total Tokens',
        'stats.success': 'Success',
        'stats.failed': 'Failed',
        'stats.avg_duration': 'Avg Duration (ms)',
        'stats.cache_hit_rate': 'Cache Hit Rate',
        'stats.success_rate': 'Success Rate',
        'stats.requests': 'Total Requests',
        'stats.by_model': 'By Model',
        'stats.by_account': 'By Account',
        'stats.model': 'Model',
        'stats.account': 'Account',
        'stats.pct': '%',
        'stats.no_data': 'No data',
        'stats.loading': 'Loading...',
        'filter.today': 'Today',
        'filter.7d': '7 Days',
        'filter.30d': '30 Days',
        'filter.custom': 'Custom',
        'filter.apply': 'Apply',
        'filter.to': 'to',
        'filter.now': 'now',
        'logs.title': 'Request History',
        'logs.time': 'Time',
        'logs.original_model': 'Original Model',
        'logs.mapped_model': 'Mapped Model',
        'logs.account': 'Account',
        'logs.thinking': 'Thinking',
        'logs.effort': 'Effort',
        'logs.tools': 'Tools',
        'logs.duration': 'Duration (ms)',
        'logs.status': 'Status',
        'logs.no_data': 'No history',
        'quotas.title': 'OpenCode Go Quotas',
        'quotas.rolling': '5-Hour Rolling',
        'quotas.weekly': 'Weekly',
        'quotas.monthly': 'Monthly',
        'quotas.not_configured': 'Quota tracking not configured. Add workspace credentials in Configuration -> API Keys.',
        'quotas.error': 'Failed to load quota data.',
        'quotas.last_updated': 'Last updated: ',
        'quotas.resets_in': 'Resets in ',
        'quotas.resetting': 'Resetting...',
        'quotas.remaining': '% remaining',
        'config.server': 'Server Settings',
        'config.api_port': 'API Port',
        'config.web_port': 'Web UI Port',
        'config.bind_address': 'Bind Address',
        'config.bind_lan': 'Local network (0.0.0.0)',
        'config.bind_local': 'Local machine only (127.0.0.1)',
        'config.key_routing': 'Key Routing',
        'config.routing_rr': 'Round-Robin',
        'config.routing_failover': 'Failover',
        'config.save': 'Save Configuration',
        'config.saved': 'Configuration updated.',
        'proxy.running': 'Running',
        'proxy.stopped': 'Stopped',
        'proxy.start': 'Start',
        'proxy.stop': 'Stop',
        'proxy.restart': 'Restart',
        'proxy.full_restart': 'Full Restart',
        'proxy.full_restart_confirm': 'Full restart will reload all code changes. The server will be briefly unavailable. Continue?',
        'proxy.restart_notice': 'Proxy restart required for port changes to take effect.',
        'proxy.copy': 'Click to copy',
        'proxy.copied': 'Copied!',
        'config.model_mapping': 'Model Mapping',
        'config.mapping_desc': 'Routes map Claude model names (opus, sonnet, haiku) to backend models.',
        'config.disable_mapping': 'Disable model mapping (pass-through)',
        'config.disable_mapping_hint': 'When enabled, the original model name is passed directly without mapping.',
        'config.opus': 'Opus Route',
        'config.sonnet': 'Sonnet Route',
        'config.haiku': 'Haiku Route',
        'config.custom_routes': 'Custom Mapping',
        'config.custom_routes_desc': 'Add custom model name to backend model mappings. Match keywords are checked in order.',
        'config.cr_match': 'Match Keyword',
        'config.cr_backend': 'Backend Model',
        'config.cr_enabled': 'Enabled',
        'config.cr_thinking': 'Thinking',
        'config.cr_effort': 'Effort',
        'config.cr_thinking_auto': 'Auto',
        'config.cr_thinking_disabled': 'Disabled',
        'config.cr_thinking_adaptive': 'Adaptive',
        'config.cr_effort_auto': 'Auto',
        'config.cr_effort_low': 'Low',
        'config.cr_effort_medium': 'Medium',
        'config.cr_effort_high': 'High',
        'config.cr_add_btn': 'Add',
        'config.cr_save': 'Save Custom Routes',
        'config.cr_saved': 'Saved!',
        'config.cr_no_routes': 'No custom routes',
        'config.new_badge': 'NEW',
        'config.cr_placeholder': 'e.g. nimo',
        'config.cr_alert': 'Enter match keyword and select a backend model.',
        'config.tool_routing': 'Tool Routing',
        'config.tool_routing_desc': 'Assign a backend model to each tool detected in recent requests. Routes are created automatically as custom mappings.',
        'config.tr_tool': 'Tool',
        'config.tr_count': 'Uses',
        'config.tr_backend': 'Backend Model',
        'config.tr_status': 'Status',
        'config.tr_routed': 'Routed',
        'config.tr_unrouted': 'Unrouted',
        'config.tr_save': 'Save Tool Routes',
        'config.tr_saved': 'Tool routes saved!',
        'config.tr_no_tools': 'No tools detected in recent requests.',
        'config.tr_alert_save': 'No tools to route.',
        'config.cr_select_model': 'Select model...',
        'config.debug_mode': 'Debug Mode',
        'config.debug_desc': 'Enable verbose request/response logging. Logs persist across restarts.',
        'config.debug_enabled': 'Debug logging active',
        'config.debug_off': 'Debug logging off',
        'config.debug_save': 'Toggle',
        'nav.debug_logs': 'Debug Logs',
        'debug.title': 'Debug Log Viewer',
        'debug.clear': 'Clear Log',
        'debug.cleared': 'Log cleared.',
        'debug.auto_refresh': 'Auto-refresh',
        'debug.no_data': 'No debug logs. Enable debug mode in Configuration first.',
        'debug.loading': 'Loading...',
        'debug.lines': 'lines',
        'config.api_keys': 'API Keys',
        'config.api_keys_desc': 'Configure API keys for upstream access. Each key can have its own Go workspace credentials.',
        'config.ak_alias': 'Alias',
        'config.ak_key': 'API Key',
        'config.ak_workspace': 'Go Workspace ID',
        'config.ak_cookie': 'Go Auth Cookie',
        'config.ak_add': '+ Add Key',
        'config.ak_save': 'Save API Keys',
        'config.ak_saved': 'Saved!',
        'config.ak_no_keys': 'No API keys configured',
        'config.ak_enabled': 'Enabled',
        'config.ak_delete': 'Delete',
        'config.models': 'Available Models',
        'config.model_id': 'Model ID',
        'config.capabilities': 'Capabilities',
        'config.protocol': 'Protocol',
        'config.endpoint': 'Endpoint',
        'config.limit_5h': '5h limit',
        'config.limit_weekly': 'Weekly limit',
        'config.limit_monthly': 'Monthly limit',
        'config.models_hint': 'Estimated request limits based on your plan\'s dollar caps and per-model pricing.',
        'config.see_docs': 'See docs',
        'config.no_models': 'No models',
        'page.of': 'Page {c} of {t}',
        'page.prev': '« Previous',
        'page.next': 'Next »',
        'delete.history': 'Delete History',
        'delete.all': 'Delete All',
        'delete.before_date': 'Delete Before Date',
        'delete.modal_title': 'Delete History Before',
        'delete.confirm_all': 'Delete all history?',
        'delete.confirm_before': 'Delete history before {d}?',
        'lang.en': 'English',
        'lang.fr': 'Français',
        'show_hide': 'Show/hide',
        'cap.chat': 'Chat',
        'cap.vision': 'Vision',
        'cap.tools': 'Tools',
        'cap.code': 'Code',
        'cap.web_search': 'Web',
        'status.error': 'Error',
        'cancel': 'Cancel',
        'error.unknown': 'Unknown error',
        'chart.token_dist': 'Token Distribution',
        'chart.token_pct': 'Token % by Model',
        'chart.req_pct': 'Requests % by Model',
    },
    fr: {
        'nav.stats': 'Statistiques',
        'nav.logs': 'Historique',
        'nav.quotas': 'Quotas',
        'nav.free_models': 'Modèles Gratuits',
        'nav.config': 'Configuration',
        'free_models.title': 'Modèles Gratuits',
        'free_models.hint': 'Gestion des accès aux modèles gratuits',
        'free_models.enable': 'Activer les modèles gratuits',
        'free_models.proxy_mode': 'Mode proxy',
        'free_models.mode_vpn': 'VPN (OpenVPN)',
        'free_models.mode_socks5': 'SOCKS5',
        'free_models.mode_direct': 'Direct',
        'free_models.socks5_title': 'Proxies SOCKS5',
        'free_models.socks5_hint': 'Liste des proxies pour routage des requêtes gratuites',
        'free_models.socks5_add': 'Ajouter proxy',
        'free_models.socks5_test': 'Tester',
        'free_models.socks5_host': 'Hôte',
        'free_models.socks5_port': 'Port',
        'free_models.socks5_user': 'Utilisateur (optionnel)',
        'free_models.socks5_pass': 'Mot de passe (optionnel)',
        'free_models.socks5_testing': 'Test en cours...',
        'free_models.socks5_ok': 'Connexion OK',
        'free_models.socks5_fail': 'Échec',
        'free_models.socks5_remove': 'Supprimer ce proxy ?',
        'free_models.socks5_rotation': 'Rotation auto entre les proxies',
        'free_models.socks5_rotate_on': 'Rotation activée',
        'free_models.socks5_rotate_off': 'Rotation désactivée',
        'vpn.title': 'VPN & Rotation d\'IP',
        'vpn.hint': 'Rotation des adresses IP pour le quota gratuit via OpenVPN',
        'vpn.status': 'Statut',
        'vpn.current_ip': 'IP actuelle',
        'vpn.current_server': 'Serveur',
        'vpn.requests_this_ip': 'Requêtes cette IP',
        'vpn.total_free': 'Total requêtes gratuites',
        'vpn.enable': 'Activer la rotation VPN',
        'vpn.enabled': 'Rotation VPN activée',
        'vpn.disabled': 'Rotation VPN désactivée',
        'vpn.connect': 'Connecter',
        'vpn.switch_ip': 'Changer d\'IP',
        'vpn.disconnect': 'Déconnecter',
        'vpn.servers': 'Serveurs VPN',
        'vpn.add_server': 'Ajouter',
        'vpn.ip_history': 'Historique des IP',
        'vpn.credentials': 'Identifiants NordVPN',
        'vpn.credentials_hint': 'Identifiants de service depuis le dashboard NordVPN (Configuration manuelle)',
        'vpn.save_credentials': 'Enregistrer',
        'vpn.browse': 'Parcourir',
        'vpn.credentials_saved': 'Identifiants enregistrés',
        'vpn.credentials_missing': 'Aucun identifiant enregistré',
        'vpn.connected': 'Connecté',
        'vpn.connecting': 'Connexion...',
        'vpn.disconnected': 'Déconnecté',
        'vpn.error': 'Erreur',
        'vpn.rotation': 'Rotation',
        'vpn.rotation_on': 'Activé',
        'vpn.rotation_off': 'Désactivé',
        'vpn.ips_used': 'IPs utilisées',
        'vpn.switches': 'Changements',
        'vpn.servers_hint': 'Configs .ovpn NordVPN',
        'vpn.confirm_remove': 'Supprimer ce serveur ?',
        'vpn.server_added': 'Serveur ajouté',
        'vpn.api_discovery': 'Découverte API NordVPN',
        'vpn.api_hint': 'Découvre automatiquement les serveurs via l\'API NordVPN',
        'vpn.advanced': 'Paramètres avancés',
        'vpn.health_check': 'Health Check',
        'vpn.config': 'Configuration VPN',
        'vpn.mode': 'Mode',
        'vpn.proxy_port': 'Port proxy',
        'vpn.switch_delay': 'Délai switch (s)',
        'vpn.docker_image': 'Image Docker',
        'vpn.config_summary': 'Config sauvée automatiquement',
        'vpn.free_map': 'Mapping gratuit',
        'vpn.free_map_desc': 'Associe chaque modèle payant à son équivalent gratuit (hot-reload, pas de restart).',
        'vpn.rotation_rules': 'Règles de rotation',
        'vpn.rotation_rules_desc': 'Règles de rotation par modèle (hot-reload).',
        'vpn.schedule': 'Planning',
        'vpn.schedule_desc': 'Planning cron pour rotation (hot-reload).',
        'vpn.schedule_enabled': 'Activé',
        'last.update': 'Dernière mise à jour : ',
        'stats.overview': 'Aperçu',
        'stats.input': 'Entrée',
        'stats.output': 'Sortie',
        'stats.cache': 'Cache',
        'stats.total': 'Total Tokens',
        'stats.success': 'Succès',
        'stats.failed': 'Échecs',
        'stats.avg_duration': 'Durée moy. (ms)',
        'stats.cache_hit_rate': 'Cache Hit Rate',
        'stats.success_rate': 'Taux de Succès',
        'stats.requests': 'Total Requêtes',
        'stats.by_model': 'Par Modèle',
        'stats.by_account': 'Par Compte',
        'stats.model': 'Modèle',
        'stats.account': 'Compte',
        'stats.pct': ' %',
        'stats.no_data': 'Aucune donnée',
        'stats.loading': 'Chargement...',
        'filter.today': "Aujourd'hui",
        'filter.7d': '7 Jours',
        'filter.30d': '30 Jours',
        'filter.custom': 'Personnalisé',
        'filter.apply': 'Appliquer',
        'filter.to': 'au',
        'filter.now': 'maintenant',
        'logs.title': 'Historique des Requêtes',
        'logs.time': 'Heure',
        'logs.original_model': "Modèle d'origine",
        'logs.mapped_model': 'Modèle mappé',
        'logs.account': 'Compte',
        'logs.thinking': 'Réflexion',
        'logs.effort': 'Effort',
        'logs.duration': 'Durée (ms)',
        'logs.status': 'Statut',
        'logs.no_data': 'Aucun historique',
        'quotas.title': 'Quotas OpenCode Go',
        'quotas.rolling': 'Glissant 5h',
        'quotas.weekly': 'Hebdomadaire',
        'quotas.monthly': 'Mensuel',
        'quotas.not_configured': 'Suivi des quotas non configuré. Ajoutez des identifiants dans Configuration → API Keys.',
        'quotas.error': 'Échec du chargement des quotas.',
        'quotas.last_updated': 'Dernière mise à jour : ',
        'quotas.resets_in': 'Réinitialisation dans ',
        'quotas.resetting': 'Réinitialisation...',
        'quotas.remaining': '% restant',
        'config.server': 'Paramètres Serveur',
        'config.api_port': 'Port API',
        'config.web_port': 'Port Interface Web',
        'config.bind_address': 'Adresse de liaison',
        'config.bind_lan': 'Réseau local (0.0.0.0)',
        'config.bind_local': 'Machine uniquement (127.0.0.1)',
        'config.key_routing': 'Routage des clés',
        'config.routing_rr': 'Round-Robin',
        'config.routing_failover': 'Failover',
        'config.save': 'Sauvegarder',
        'config.saved': 'Configuration mise à jour.',
        'proxy.running': 'En cours',
        'proxy.stopped': 'Arrêté',
        'proxy.start': 'Démarrer',
        'proxy.stop': 'Arrêter',
        'proxy.restart': 'Redémarrer',
        'proxy.full_restart': 'Redémarrage complet',
        'proxy.full_restart_confirm': 'Le redémarrage complet rechargera toutes les modifications de code. Le serveur sera brièvement indisponible. Continuer ?',
        'proxy.restart_notice': 'Un redémarrage est nécessaire pour appliquer les changements de port.',
        'proxy.copy': 'Cliquer pour copier',
        'proxy.copied': 'Copié !',
        'config.model_mapping': 'Mapping des Modèles',
        'config.mapping_desc': 'Les routes associent les noms Claude (opus, sonnet, haiku) aux modèles backend.',
        'config.disable_mapping': 'Désactiver le mapping (transparent)',
        'config.disable_mapping_hint': 'Quand activé, le nom du modèle original est passé directement.',
        'config.opus': 'Route Opus',
        'config.sonnet': 'Route Sonnet',
        'config.haiku': 'Route Haiku',
        'config.custom_routes': 'Mapping Personnalisé',
        'config.custom_routes_desc': 'Ajoutez des mappings personnalisés (vérifiés dans l\'ordre).',
        'config.cr_match': 'Mot-clé',
        'config.cr_backend': 'Modèle Backend',
        'config.cr_enabled': 'Active',
        'config.cr_thinking': 'Réflexion',
        'config.cr_effort': 'Effort',
        'config.cr_thinking_auto': 'Auto',
        'config.cr_thinking_disabled': 'Désactivé',
        'config.cr_thinking_adaptive': 'Adaptatif',
        'config.cr_effort_auto': 'Auto',
        'config.cr_effort_low': 'Faible',
        'config.cr_effort_medium': 'Moyen',
        'config.cr_effort_high': 'Élevé',
        'config.cr_add_btn': 'Ajouter',
        'config.cr_save': 'Sauvegarder',
        'config.cr_saved': 'Sauvegardé !',
        'config.cr_no_routes': 'Aucun routage personnalisé',
        'config.new_badge': 'NOUVEAU',
        'config.cr_placeholder': 'ex: nimo',
        'config.cr_alert': 'Entrez un mot-clé et sélectionnez un modèle.',
        'config.tool_routing': 'Routage par Outil',
        'config.tool_routing_desc': 'Assignez un modèle backend à chaque outil détecté dans les requêtes récentes. Les routes sont créées automatiquement.',
        'config.tr_tool': 'Outil',
        'config.tr_count': 'Utilisations',
        'config.tr_backend': 'Modèle Backend',
        'config.tr_status': 'Statut',
        'config.tr_routed': 'Routé',
        'config.tr_unrouted': 'Non routé',
        'config.tr_save': 'Sauvegarder',
        'config.tr_saved': 'Routes d\'outils sauvegardées !',
        'config.tr_no_tools': 'Aucun outil détecté dans les requêtes récentes.',
        'config.tr_alert_save': 'Rien à sauvegarder.',
        'config.cr_select_model': 'Sélectionner un modèle...',
        'config.debug_mode': 'Mode Debug',
        'config.debug_desc': 'Activer la journalisation détaillée des requêtes/réponses. Les logs persistent après redémarrage.',
        'config.debug_enabled': 'Debug actif',
        'config.debug_off': 'Debug désactivé',
        'config.debug_save': 'Basculer',
        'nav.debug_logs': 'Logs Debug',
        'debug.title': 'Visionneuse de Logs Debug',
        'debug.clear': 'Vider le log',
        'debug.cleared': 'Log vidé.',
        'debug.auto_refresh': 'Actualisation auto',
        'debug.no_data': 'Aucun log debug. Activez le mode debug dans Configuration.',
        'debug.loading': 'Chargement...',
        'debug.lines': 'lignes',
        'config.api_keys': 'Clés API',
        'config.api_keys_desc': 'Configurez les clés API. Chaque clé peut avoir ses propres identifiants Go workspace.',
        'config.ak_alias': 'Alias',
        'config.ak_key': 'Clé API',
        'config.ak_workspace': 'ID Workspace',
        'config.ak_cookie': 'Cookie Auth',
        'config.ak_add': '+ Ajouter',
        'config.ak_save': 'Sauvegarder',
        'config.ak_saved': 'Sauvegardé !',
        'config.ak_no_keys': 'Aucune clé API configurée',
        'config.ak_enabled': 'Activée',
        'config.ak_delete': 'Supprimer',
        'config.models': 'Modèles Disponibles',
        'config.model_id': 'ID du Modèle',
        'config.capabilities': 'Capacités',
        'config.protocol': 'Protocole',
        'config.endpoint': 'Endpoint',
        'config.limit_5h': 'Limite 5h',
        'config.limit_weekly': 'Limite hebdo',
        'config.limit_monthly': 'Limite mensuelle',
        'config.models_hint': 'Limites estimées selon votre plan et le tarif par modèle.',
        'config.see_docs': 'Voir docs',
        'config.no_models': 'Aucun modèle',
        'page.of': 'Page {c} sur {t}',
        'page.prev': '« Précédent',
        'page.next': 'Suivant »',
        'delete.history': 'Supprimer',
        'delete.all': 'Tout supprimer',
        'delete.before_date': 'Supprimer avant le',
        'delete.modal_title': 'Supprimer l\'historique avant le',
        'delete.confirm_all': 'Supprimer tout l\'historique ?',
        'delete.confirm_before': 'Supprimer l\'historique avant le {d} ?',
        'lang.en': 'English',
        'lang.fr': 'Français',
        'show_hide': 'Afficher/masquer',
        'cap.chat': 'Chat',
        'cap.vision': 'Vision',
        'cap.tools': 'Outils',
        'cap.code': 'Code',
        'cap.web_search': 'Web',
        'status.error': 'Erreur',
        'cancel': 'Annuler',
        'error.unknown': 'Erreur inconnue',
        'chart.token_dist': 'Distribution des tokens',
        'chart.token_pct': 'Tokens % par modèle',
        'chart.req_pct': 'Requêtes % par modèle',
    },
};

let _lang = localStorage.getItem('lang') || 'en';

function t(key) {
    return LOCALE[_lang]?.[key] || LOCALE.en[key] || key;
}

function setLang(lang) {
    _lang = lang;
    localStorage.setItem('lang', lang);
    document.documentElement.lang = lang;
    // Re-translate static elements
    document.querySelectorAll('[data-i18n]').forEach(el => {
        el.textContent = t(el.dataset.i18n);
    });
    // Refresh views
    const activeTab = document.querySelector('.tab.active');
    if (activeTab) {
        const target = activeTab.dataset.tab;
        if (target === 'config') fetchConfig().then(renderConfig);
        else if (target === 'quotas') fetchQuotas().then(renderQuotas);
    }
    refreshAll();
}

function getLang() {
    return _lang;
}

function makeRouteKey(match) {
    if (match === '*') return '*';
    return match.toLowerCase().replace(/[^a-z0-9]/g, '');
}

function getErrorDetails(errorStr) {
    if (!errorStr) return { short: t('error.unknown'), explanation: '' };
    const match = errorStr.match(/HTTP\s+(\d{3})/i);
    if (match) {
        const code = parseInt(match[1]);
        const explanation = HTTP_STATUS_EXPLANATIONS[code];
        return { short: errorStr, explanation: explanation || `HTTP status code ${code}` };
    }
    return { short: errorStr, explanation: '' };
}

function formatNumber(num) {
    return num ? num.toLocaleString() : '0';
}

function formatTime() {
    return new Date().toLocaleTimeString();
}

function formatDateTime(isoString) {
    if (!isoString) return '-';
    return new Date(isoString).toLocaleString();
}

function todayStr() {
    return new Date().toLocaleDateString('en-CA');
}

function daysAgoStr(n) {
    const d = new Date();
    d.setDate(d.getDate() - n);
    return d.toLocaleDateString('en-CA');
}

// Global filter state
let filterFrom = todayStr();
let filterTo = todayStr();
let filterStatus = '';
let filterModel = '';
let filterOriginalModel = '';
let filterAccount = '';
let filterTool = '';
let filterSearch = '';
let filterStream = '';

// Pagination state
let currentPage = 1;
const perPage = 20;
let totalPages = 1;

// Config state
let configData = null;
let availableModels = [];

function escHtml(s) {
    return (s || '').replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;').replace(/"/g, '&quot;').replace(/'/g, '&#39;');
}

async function apiFetch(url, options = {}) {
    const resp = await fetch(url, options);
    if (!resp.ok) {
        const text = await resp.text().catch(() => '');
        throw new Error(`HTTP ${resp.status}: ${text.slice(0, 200)}`);
    }
    return resp.json();
}

async function fetchStats(from, to) {
    try {
        let url = '/api/stats?';
        if (from) url += `from_date=${from}&`;
        if (to) url += `to_date=${to}`;
        return await apiFetch(url);
    } catch (e) {
        console.error('Failed to fetch stats:', e);
        return null;
    }
}

async function fetchToolRoutes(days = 7, showAll = false) {
    try {
        const params = `days=${days}` + (showAll ? '&all=true' : '');
        return await apiFetch(`/api/tools?${params}`);
    } catch (e) {
        console.error('Failed to fetch tool routes:', e);
        return [];
    }
}

async function fetchHistory(from, to, page = 1) {
    try {
        const offset = (page - 1) * perPage;
        let url = `/api/history?limit=${perPage}&offset=${offset}`;
        if (from) url += `&from_date=${from}`;
        if (to) url += `&to_date=${to}`;
        if (filterStatus) url += `&status=${encodeURIComponent(filterStatus)}`;
        if (filterModel) url += `&model=${encodeURIComponent(filterModel)}`;
        if (filterOriginalModel) url += `&original_model=${encodeURIComponent(filterOriginalModel)}`;
        if (filterAccount) url += `&account=${encodeURIComponent(filterAccount)}`;
        if (filterTool) url += `&tool=${encodeURIComponent(filterTool)}`;
        if (filterSearch) url += `&search=${encodeURIComponent(filterSearch)}`;
        if (filterStream) url += `&is_stream=${encodeURIComponent(filterStream)}`;
        return await apiFetch(url);
    } catch (e) {
        console.error('Failed to fetch history:', e);
        return null;
    }
}

async function fetchConfig() {
    try {
        configData = await apiFetch('/api/config');
        return configData;
    } catch (e) {
        console.error('Failed to fetch config:', e);
        return null;
    }
}

async function fetchQuotas() {
    try {
        return await apiFetch('/api/quotas');
    } catch (e) {
        console.error('Failed to fetch quotas:', e);
        return null;
    }
}

async function fetchDebugStatus() {
    try {
        return await apiFetch('/api/debug');
    } catch (e) {
        console.error('Failed to fetch debug status:', e);
        return { enabled: false };
    }
}

async function setDebugMode(enabled) {
    try {
        return await apiFetch('/api/debug', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ enabled }),
        });
    } catch (e) {
        console.error('Failed to set debug mode:', e);
        return { enabled: false };
    }
}

// Debug logs pagination state
let debugLogPage = 1;
const debugLogPerPage = 200;
let debugLogTotalPages = 1;

async function fetchDebugLogs(page = 1) {
    try {
        const offset = (page - 1) * debugLogPerPage;
        return await apiFetch(`/api/debug/logs?limit=${debugLogPerPage}&offset=${offset}`);
    } catch (e) {
        console.error('Failed to fetch debug logs:', e);
        return { logs: [], total: 0, has_more: false };
    }
}

async function clearDebugLogs() {
    try {
        return await apiFetch('/api/debug/logs', { method: 'DELETE' });
    } catch (e) {
        console.error('Failed to clear debug logs:', e);
        return { status: 'error' };
    }
}

function renderDebugLogLines(data) {
    const el = document.getElementById('debug-log-content');
    if (!data || !data.logs || data.logs.length === 0) {
        el.textContent = t('debug.no_data');
        el.style.color = 'var(--text-dim)';
        return;
    }
    el.style.color = '';
    el.textContent = data.logs.join('\n');
    // Scroll to bottom (most recent at bottom after reverse in API)
    el.scrollTop = el.scrollHeight;
    // Update pagination
    debugLogTotalPages = Math.max(1, Math.ceil(data.total / debugLogPerPage));
    const pageInfo = document.getElementById('debug-page-info');
    const prevBtn = document.getElementById('debug-prev-page');
    const nextBtn = document.getElementById('debug-next-page');
    pageInfo.textContent = t('page.of').replace('{c}', debugLogPage).replace('{t}', debugLogTotalPages);
    prevBtn.disabled = debugLogPage <= 1;
    nextBtn.disabled = debugLogPage >= debugLogTotalPages;
}

function formatResetTime(seconds) {
    if (seconds <= 0) return t('filter.now');
    const d = Math.floor(seconds / 86400);
    const h = Math.floor((seconds % 86400) / 3600);
    const m = Math.floor((seconds % 3600) / 60);
    const s = Math.floor(seconds % 60);
    if (d > 0) return `${d}d ${h}h ${m}m`;
    if (h > 0) return `${h}h ${m}m`;
    if (m > 0) return `${m}m ${s}s`;
    return `${s}s`;
}

function renderQuotas(data) {
    const container = document.getElementById('quota-workspaces');
    const statusMsg = document.getElementById('quota-status-message');

    if (!data || Object.keys(data).length === 0) {
        statusMsg.textContent = t('quotas.not_configured');
        container.innerHTML = '';
        return;
    }

    statusMsg.textContent = '';
    const entries = Object.entries(data);
    const showHeaders = entries.length > 1;

    // Build workspace ID → alias lookup from config
    const wsAliasMap = {};
    if (configData && configData.api_keys) {
        for (const k of configData.api_keys) {
            if (k.go_workspace_id && k.alias) wsAliasMap[k.go_workspace_id] = k.alias;
        }
    }

    let allHtml = '';
    for (const [wsId, wsData] of entries) {
        const status = wsData.status || 'error';
        const error = wsData.error || '';
        const quotas = wsData.quotas || {};
        const fetchedAt = wsData.fetched_at || null;
        const alias = wsAliasMap[wsId] || '';

        if (status === 'error' && !quotas.rolling && !quotas.weekly && !quotas.monthly) {
            const label = alias || wsId.slice(0, 8) + '...';
            allHtml += `<div class="quota-workspace">
                <div class="quota-error">${escHtml(label)}: ${t('status.error')} — ${error}</div>
            </div>`;
            continue;
        }

        let wsHtml = '';
        if (showHeaders) {
            const displayName = alias || wsId;
            const tooltip = alias ? ` title="${escHtml(wsId)}"` : '';
            wsHtml += `<h3 class="quota-workspace-title"${tooltip}>Workspace: ${escHtml(displayName)}</h3>`;
        }

        // Per-workspace status message
        if (status === 'error') {
            wsHtml += `<p class="config-hint" style="color:var(--danger)">${t('quotas.last_updated')}${error}.</p>`;
        }

        wsHtml += '<div class="quota-grid">';
        for (const period of ['rolling', 'weekly', 'monthly']) {
            const q = quotas[period] || { usage_percent: 0, reset_in_sec: 0 };
            const pct = Math.min(100, Math.max(0, q.usage_percent || 0));
            const remaining = (100 - pct).toFixed(1);

            const barClass = pct >= 85 ? 'quota-bar-danger' : pct >= 60 ? 'quota-bar-warning' : 'quota-bar-ok';
            const label = period === 'rolling' ? t('quotas.rolling') : period === 'weekly' ? t('quotas.weekly') : t('quotas.monthly');
            const resetSec = q.reset_in_sec || 0;
            const resetText = resetSec > 0 ? t('quotas.resets_in') + formatResetTime(resetSec) : '';
            const resetTarget = resetSec > 0 ? Math.floor(Date.now() / 1000) + resetSec : '';

            wsHtml += `<div class="quota-item">
                <div class="quota-header">
                    <span class="quota-label">${label}</span>
                    <span class="quota-reset" data-reset-target="${resetTarget}">${resetText}</span>
                </div>
                <div class="quota-bar-container">
                    <div class="quota-bar ${barClass}" style="width:${pct}%"></div>
                </div>
                <div class="quota-stats">
                    <span class="quota-usage">${pct.toFixed(1)}%</span>
                    <span class="quota-remaining">${remaining}${t('quotas.remaining')}</span>
                </div>
            </div>`;
        }
        wsHtml += '</div>';

        if (fetchedAt) {
            wsHtml += `<div class="quota-footer"><span class="quota-fetched-at">${t('quotas.last_updated')}${formatDateTime(fetchedAt)}</span></div>`;
        }

        allHtml += `<div class="quota-workspace">${wsHtml}</div>`;
    }

    container.innerHTML = allHtml;
}

function renderStats(data) {
    if (!data) return;

    const totals = data.totals;
    document.getElementById('total-input').textContent = formatNumber(totals.input);
    document.getElementById('total-output').textContent = formatNumber(totals.output);
    document.getElementById('total-cache').textContent = formatNumber(totals.cache);
    document.getElementById('total-all').textContent = formatNumber(totals.total);
    document.getElementById('total-success').textContent = formatNumber(totals.success_count);
    document.getElementById('total-fail').textContent = formatNumber(totals.fail_count);
    document.getElementById('avg-duration').textContent = totals.avg_duration_ms ? formatNumber(totals.avg_duration_ms) : '-';
    document.getElementById('cache-hit-rate').textContent = totals.cache_hit_rate != null ? totals.cache_hit_rate + '%' : '0%';
    document.getElementById('success-rate').textContent = totals.success_rate != null ? totals.success_rate + '%' : '0%';
    document.getElementById('total-requests').textContent = formatNumber(totals.count);

    const tbody = document.getElementById('model-tbody');
    const models = data.models;

    if (Object.keys(models).length === 0) {
        tbody.innerHTML = '<tr><td colspan="11">' + t('stats.no_data') + '</td></tr>';
    } else {
        let html = '';
        for (const [model, s] of Object.entries(models)) {
            html += `<tr>
                <td>${escHtml(model)}</td>
                <td>${formatNumber(s.input)}</td>
                <td>${formatNumber(s.output)}</td>
                <td>${formatNumber(s.cache)}</td>
                <td>${formatNumber(s.total)}</td>
                <td>${s.pct}</td>
                <td>${s.cache_hit_rate != null ? s.cache_hit_rate + '%' : '0%'}</td>
                <td>${s.success_rate != null ? s.success_rate + '%' : '0%'}</td>
                <td>${formatNumber(s.success_count)}</td>
                <td>${formatNumber(s.fail_count)}</td>
                <td>${s.avg_duration_ms ? formatNumber(s.avg_duration_ms) : '-'}</td>
            </tr>`;
        }
        tbody.innerHTML = html;
    }

    // Per-account stats
    const atbody = document.getElementById('account-tbody');
    const accounts = data.accounts;
    if (!accounts || Object.keys(accounts).length === 0) {
        atbody.innerHTML = '<tr><td colspan="11">' + t('stats.no_data') + '</td></tr>';
    } else {
        let html = '';
        for (const [account, s] of Object.entries(accounts)) {
            html += `<tr>
                <td>${escHtml(account)}</td>
                <td>${formatNumber(s.input)}</td>
                <td>${formatNumber(s.output)}</td>
                <td>${formatNumber(s.cache)}</td>
                <td>${formatNumber(s.total)}</td>
                <td>${s.pct}</td>
                <td>${s.cache_hit_rate != null ? s.cache_hit_rate + '%' : '0%'}</td>
                <td>${s.success_rate != null ? s.success_rate + '%' : '0%'}</td>
                <td>${formatNumber(s.success_count)}</td>
                <td>${formatNumber(s.fail_count)}</td>
                <td>${s.avg_duration_ms ? formatNumber(s.avg_duration_ms) : '-'}</td>
            </tr>`;
        }
        atbody.innerHTML = html;
    }
}

let chartTokens = null;
let chartModelTokens = null;
let chartModelRequests = null;

const CHART_COLORS = ['#4fc3f7', '#ff8a65', '#81c784', '#ba68c8', '#ffd54f', '#f06292', '#4dd0e1', '#aed581'];

function makeChartOpts(textColor) {
    return { responsive: true, plugins: { legend: { labels: { color: textColor } } } };
}

function renderCharts(data) {
    if (!data) return;
    const isDark = document.documentElement.getAttribute('data-theme') !== 'light';
    const textColor = isDark ? '#e0e0e0' : '#333';

    const totals = data.totals;
    const tokenData = [totals.input, totals.output, totals.cache];

    // Token distribution
    if (chartTokens) {
        chartTokens.data.datasets[0].data = tokenData;
        chartTokens.options.plugins.legend.labels.color = textColor;
        chartTokens.update('none');
    } else {
        try {
            chartTokens = new Chart(document.getElementById('chart-tokens'), {
                type: 'doughnut',
                data: {
                    labels: ['Input', 'Output', 'Cache'],
                    datasets: [{ data: tokenData, backgroundColor: ['#4fc3f7', '#ff8a65', '#81c784'], borderWidth: 0 }]
                },
                options: makeChartOpts(textColor)
            });
        } catch (e) {
            console.warn('Chart.js not available:', e);
            document.querySelector('.chart-card')?.remove();
        }
    }

    // Per model
    const models = Object.entries(data.models);
    const modelLabels = models.map(([m]) => m);
    const modelTokenData = models.map(([, s]) => s.total);
    const modelRequestData = models.map(([, s]) => s.count);
    const colors = CHART_COLORS.slice(0, modelLabels.length);

    if (chartModelTokens) {
        chartModelTokens.data.labels = modelLabels;
        chartModelTokens.data.datasets[0].data = modelTokenData;
        chartModelTokens.data.datasets[0].backgroundColor = colors;
        chartModelTokens.options.plugins.legend.labels.color = textColor;
        chartModelTokens.update('none');
    } else {
        try {
            chartModelTokens = new Chart(document.getElementById('chart-model-tokens'), {
                type: 'doughnut',
                data: {
                    labels: modelLabels,
                    datasets: [{ data: modelTokenData, backgroundColor: colors, borderWidth: 0 }]
                },
                options: makeChartOpts(textColor)
            });
        } catch (e) {
            console.warn('Chart.js not available:', e);
        }
    }

    if (chartModelRequests) {
        chartModelRequests.data.labels = modelLabels;
        chartModelRequests.data.datasets[0].data = modelRequestData;
        chartModelRequests.data.datasets[0].backgroundColor = colors;
        chartModelRequests.options.plugins.legend.labels.color = textColor;
        chartModelRequests.update('none');
    } else {
        try {
            chartModelRequests = new Chart(document.getElementById('chart-model-requests'), {
                type: 'doughnut',
                data: {
                    labels: modelLabels,
                    datasets: [{ data: modelRequestData, backgroundColor: colors, borderWidth: 0 }]
                },
                options: makeChartOpts(textColor)
            });
        } catch (e) {
            console.warn('Chart.js not available:', e);
        }
    }
}

// ── Time-series charts ──
let chartTsRequests = null, chartTsTokens = null, chartTsDuration = null, chartTsSuccess = null;

async function renderTimeSeriesCharts(from, to) {
    try {
        const data = await apiFetch(`/api/stats/timeseries?from_date=${from || ''}&to_date=${to || ''}&granularity=hour`);
        if (!data || !data.series || !data.series.length) return;

        const isDark = document.documentElement.getAttribute('data-theme') !== 'light';
        const textColor = isDark ? '#e0e0e0' : '#333';
        const gridColor = isDark ? 'rgba(255,255,255,0.1)' : 'rgba(0,0,0,0.1)';

        const labels = data.series.map(s => {
            const d = s.period;
            // Format: "2026-06-05 14:00" → "14h" or "2026-06-05" → "Jun 5"
            if (d.length === 16) return d.substring(11, 16); // "HH:MM"
            if (d.length === 10) return d.substring(5); // "MM-DD"
            return d;
        });

        const tsOpts = {
            responsive: true,
            maintainAspectRatio: false,
            plugins: { legend: { display: false } },
            scales: {
                x: { ticks: { color: textColor, maxTicksLimit: 12 }, grid: { color: gridColor } },
                y: { ticks: { color: textColor }, grid: { color: gridColor }, beginAtZero: true }
            },
            elements: { point: { radius: 2 }, line: { tension: 0.3 } }
        };

        // Requests over time (bar)
        const reqData = data.series.map(s => s.count);
        const failData = data.series.map(s => s.fail);
        if (chartTsRequests) {
            chartTsRequests.data.labels = labels;
            chartTsRequests.data.datasets[0].data = reqData;
            chartTsRequests.data.datasets[1].data = failData;
            chartTsRequests.update('none');
        } else {
            try {
                chartTsRequests = new Chart(document.getElementById('chart-ts-requests'), {
                    type: 'bar',
                    data: {
                        labels,
                        datasets: [
                            { label: 'Success', data: reqData, backgroundColor: 'rgba(75,192,192,0.7)' },
                            { label: 'Errors', data: failData, backgroundColor: 'rgba(255,99,132,0.7)' }
                        ]
                    },
                    options: { ...tsOpts, plugins: { legend: { display: true, labels: { color: textColor } } } }
                });
            } catch (e) { console.warn('Chart.js not available:', e); }
        }

        // Tokens over time (line)
        const inputData = data.series.map(s => s.input_tokens);
        const outputData = data.series.map(s => s.output_tokens);
        const cacheData = data.series.map(s => s.cache_tokens);
        if (chartTsTokens) {
            chartTsTokens.data.labels = labels;
            chartTsTokens.data.datasets[0].data = inputData;
            chartTsTokens.data.datasets[1].data = outputData;
            chartTsTokens.data.datasets[2].data = cacheData;
            chartTsTokens.update('none');
        } else {
            try {
                chartTsTokens = new Chart(document.getElementById('chart-ts-tokens'), {
                    type: 'line',
                    data: {
                        labels,
                        datasets: [
                            { label: 'Input', data: inputData, borderColor: '#4fc3f7', fill: true, backgroundColor: 'rgba(79,195,247,0.1)' },
                            { label: 'Output', data: outputData, borderColor: '#ff8a65', fill: true, backgroundColor: 'rgba(255,138,101,0.1)' },
                            { label: 'Cache', data: cacheData, borderColor: '#81c784', fill: true, backgroundColor: 'rgba(129,199,132,0.1)' }
                        ]
                    },
                    options: { ...tsOpts, plugins: { legend: { display: true, labels: { color: textColor } } } }
                });
            } catch (e) { console.warn('Chart.js not available:', e); }
        }

        // Duration over time (line)
        const durData = data.series.map(s => s.avg_duration_ms);
        if (chartTsDuration) {
            chartTsDuration.data.labels = labels;
            chartTsDuration.data.datasets[0].data = durData;
            chartTsDuration.update('none');
        } else {
            try {
                chartTsDuration = new Chart(document.getElementById('chart-ts-duration'), {
                    type: 'line',
                    data: {
                        labels,
                        datasets: [{ label: 'Avg Duration (ms)', data: durData, borderColor: '#ce93d8', fill: true, backgroundColor: 'rgba(206,147,216,0.1)' }]
                    },
                    options: tsOpts
                });
            } catch (e) { console.warn('Chart.js not available:', e); }
        }

        // Success vs Error (stacked bar)
        const successData = data.series.map(s => s.success);
        if (chartTsSuccess) {
            chartTsSuccess.data.labels = labels;
            chartTsSuccess.data.datasets[0].data = successData;
            chartTsSuccess.data.datasets[1].data = failData;
            chartTsSuccess.update('none');
        } else {
            try {
                chartTsSuccess = new Chart(document.getElementById('chart-ts-success'), {
                    type: 'bar',
                    data: {
                        labels,
                        datasets: [
                            { label: 'Success', data: successData, backgroundColor: 'rgba(75,192,192,0.7)' },
                            { label: 'Error', data: failData, backgroundColor: 'rgba(255,99,132,0.7)' }
                        ]
                    },
                    options: { ...tsOpts, plugins: { legend: { display: true, labels: { color: textColor } } }, scales: { ...tsOpts.scales, x: { ...tsOpts.scales.x, stacked: true }, y: { ...tsOpts.scales.y, stacked: true } } }
                });
            } catch (e) { console.warn('Chart.js not available:', e); }
        }
    } catch (e) {
        console.error('Failed to render time-series charts:', e);
    }
}

// ── Request Detail Modal ──

let _historyLogsCache = {};

function showRequestDetail(reqId) {
    const log = _historyLogsCache[reqId];
    if (!log) return;

    const modal = document.getElementById('request-detail-modal');
    const content = document.getElementById('req-detail-content');

    // Show loading state while fetching full details
    content.innerHTML = '<p style="color:#888;text-align:center;padding:20px">Loading details...</p>';
    modal.style.display = 'flex';

    // Fetch full details from API
    fetch(`/api/requests/${encodeURIComponent(reqId)}`)
        .then(r => r.json())
        .then(detail => {
            const statusHtml = detail.success
                ? '<span class="status-ok">&#10004; Success</span>'
                : `<span class="status-fail">&#10008; ${escHtml(detail.error || 'Error')}</span>`;

            const toolsUsedHtml = (detail.tools_used && detail.tools_used.length)
                ? [...new Set(detail.tools_used)].map(t => `<span class="tool-badge used">${escHtml(t)}</span>`).join(' ')
                : '<span style="color:#888">-</span>';

            const toolsDeclaredHtml = (detail.tools && detail.tools.length)
                ? detail.tools.map(t => `<span class="tool-badge declared">${escHtml(t)}</span>`).join(' ')
                : '<span style="color:#888">-</span>';

            // Build collapsible JSON sections
            function jsonSection(title, data, collapsed) {
                if (!data) return '';
                const jsonStr = JSON.stringify(data, null, 2);
                const size = jsonStr.length;
                const sizeLabel = size > 1024 ? `${(size/1024).toFixed(1)} KB` : `${size} B`;
                const id = 'json-' + title.toLowerCase().replace(/[^a-z0-9]+/g, '-');
                return `
                    <div class="detail-section">
                        <h4 class="json-toggle" onclick="document.getElementById('${id}').classList.toggle('collapsed'); this.classList.toggle('collapsed')">
                            ${collapsed ? '&#9654;' : '&#9660;'} ${escHtml(title)} <span class="json-size">(${sizeLabel})</span>
                        </h4>
                        <pre id="${id}" class="json-block${collapsed ? ' collapsed' : ''}"><code>${escHtml(jsonStr)}</code></pre>
                    </div>
                `;
            }

            content.innerHTML = `
                <div class="detail-grid">
                    <span class="detail-label">Status</span>
                    <span class="detail-value">${statusHtml}</span>
                    <span class="detail-label">ID</span>
                    <span class="detail-value" style="font-family:monospace;font-size:0.85em">${escHtml(detail.id)}</span>
                    <span class="detail-label">Timestamp</span>
                    <span class="detail-value">${formatDateTime(detail.timestamp)}</span>
                    <span class="detail-label">Account</span>
                    <span class="detail-value">${escHtml(detail.account_alias) || '-'}</span>
                    <span class="detail-label">Client IP</span>
                    <span class="detail-value">${escHtml(detail.client_ip) || '-'}</span>
                    <span class="detail-label">Client</span>
                    <span class="detail-value" style="font-family:monospace;font-size:0.85em">${escHtml(detail.client_user_agent) || '-'}</span>
                    <span class="detail-label">Original Model</span>
                    <span class="detail-value">${escHtml(detail.original_model) || '-'}</span>
                    <span class="detail-label">Mapped Model</span>
                    <span class="detail-value">${escHtml(detail.model) || '-'}</span>
                    <span class="detail-label">Protocol</span>
                    <span class="detail-value">${escHtml(detail.protocol) || '-'}</span>
                    <span class="detail-label">Stream</span>
                    <span class="detail-value">${detail.is_stream ? 'Yes' : 'No'}</span>
                    <span class="detail-label">Duration</span>
                    <span class="detail-value">${detail.duration_ms ? formatNumber(detail.duration_ms) + ' ms' : '-'}</span>
                    <span class="detail-label">Input Tokens</span>
                    <span class="detail-value">${formatNumber(detail.tokens_input)}</span>
                    <span class="detail-label">Output Tokens</span>
                    <span class="detail-value">${formatNumber(detail.tokens_output)}</span>
                    <span class="detail-label">Cache Tokens</span>
                    <span class="detail-value">${formatNumber(detail.tokens_cache)}</span>
                    <span class="detail-label">Thinking</span>
                    <span class="detail-value">${escHtml(detail.thinking) || '-'}</span>
                    <span class="detail-label">Effort</span>
                    <span class="detail-value">${escHtml(detail.effort) || '-'}</span>
                </div>

                <div class="detail-section">
                    <h4>Tools Used (${detail.tools_used ? [...new Set(detail.tools_used)].length : 0})</h4>
                    <div class="detail-tools-list">${toolsUsedHtml}</div>
                </div>

                <div class="detail-section">
                    <h4>Tools Declared (${detail.tools ? detail.tools.length : 0})</h4>
                    <div class="detail-tools-list">${toolsDeclaredHtml}</div>
                </div>

                ${jsonSection('Request Body', detail.request_body, false)}
                ${jsonSection('Response Body', detail.response_body, true)}
            `;
        })
        .catch(err => {
            content.innerHTML = `<p style="color:#f44">Failed to load details: ${escHtml(err.message)}</p>`;
        });
}

function renderHistory(data) {
    const tbody = document.getElementById('history-tbody');
    if (!data || data.logs.length === 0) {
        tbody.innerHTML = '<tr><td colspan="14">' + t('logs.no_data') + '</td></tr>';
        updatePagination(1, 1);
        return;
    }

    // Cache logs for detail modal
    for (const log of data.logs) {
        _historyLogsCache[log.id] = log;
    }

    let html = '';
    for (const log of data.logs) {
        const duration = log.duration_ms ? formatNumber(log.duration_ms) : '-';
        let status;
        if (log.success) {
            status = '<span class="status-ok">&#10004;</span>';
        } else {
            const errDetail = getErrorDetails(log.error || '');
            const escError = escHtml(log.error);
            const escExplanation = errDetail.explanation ? escHtml(errDetail.explanation) : '';
            // Build a rich tooltip: error → explanation → context
            const tooltipParts = ['Error: ' + escError];
            if (escExplanation) tooltipParts.push(escExplanation);
            tooltipParts.push('Model: ' + escHtml(log.model || '-') + ' | Protocol: ' + escHtml(log.protocol || '-'));
            const tooltipText = tooltipParts.join('&#10;');
            status = `<span class="status-fail">&#10008;</span> <span class="error-text">${escError}</span><span class="status-info" title="${tooltipText}">&#9432;</span>`;
        }
        const thinking = log.thinking || '-';
        const effort = log.effort || '-';
        // Tools: show badges for actually used tools
        let toolsHtml = '-';
        if (log.tools_used && log.tools_used.length) {
            const uniqueTools = [...new Set(log.tools_used)];
            const badges = uniqueTools.slice(0, 5).map(t => `<span class="tool-badge used">${escHtml(t)}</span>`).join('');
            const more = uniqueTools.length > 5 ? `<span class="tool-badge declared">+${uniqueTools.length - 5}</span>` : '';
            const tooltip = (log.tools && log.tools.length) ? `Déclarés: ${log.tools.join(', ')}\nUtilisés: ${uniqueTools.join(', ')}` : `Utilisés: ${uniqueTools.join(', ')}`;
            toolsHtml = `<span title="${escHtml(tooltip)}">${badges}${more}</span>`;
        } else if (log.tools && log.tools.length) {
            toolsHtml = `<span title="${escHtml(log.tools.join(', '))}"><span class="tool-badge declared">${log.tools.length} declared</span></span>`;
        }
        html += `<tr class="detail-row" data-req-id="${escHtml(log.id)}">
            <td>${log.is_free_model ? '<span title="Modèle gratuit — pas de compte utilisé" style="color:var(--success);font-weight:bold">' + escHtml(log.free_ip || 'GRATUIT') + '</span>' : escHtml(log.account_alias) || '-'}</td>
            <td>${formatDateTime(log.timestamp)}</td>
            <td>${escHtml(log.original_model) || '-'}</td>
            <td>${escHtml(log.model) || '-'}</td>
            <td>${formatNumber(log.tokens_input)}</td>
            <td>${formatNumber(log.tokens_output)}</td>
            <td>${formatNumber(log.tokens_cache)}</td>
            <td>${thinking}</td>
            <td>${effort}</td>
            <td>${toolsHtml}</td>
            <td>${escHtml(log.client_ip) || '-'}</td>
            <td>${log.is_stream ? '<span class="tool-badge used">Stream</span>' : '<span style="color:#888">-</span>'}</td>
            <td>${duration}</td>
            <td>${status}</td>
        </tr>`;
    }
    tbody.innerHTML = html;

    // Update pagination
    totalPages = Math.ceil(data.total / perPage);
    updatePagination(currentPage, totalPages);
}

function renderConfig(data) {
    if (!data) return;

    // Proxy status
    const statusDot = document.getElementById('proxy-status-dot');
    const statusText = document.getElementById('proxy-status-text');
    const statusAddr = document.getElementById('proxy-status-addr');
    const statusLan = document.getElementById('proxy-status-lan');
    const btnStart = document.getElementById('btn-proxy-start');
    const btnStop = document.getElementById('btn-proxy-stop');
    const btnRestart = document.getElementById('btn-proxy-restart');

    if (data.proxy_running) {
        statusDot.className = 'status-dot running';
        statusText.textContent = t('proxy.running');
        btnStart.disabled = true;
        btnStop.disabled = false;
        btnRestart.disabled = false;
    } else {
        statusDot.className = 'status-dot stopped';
        statusText.textContent = t('proxy.stopped');
        btnStart.disabled = false;
        btnStop.disabled = true;
        btnRestart.disabled = true;
    }

    // Local address — click to copy
    const localUrl = `http://localhost:${data.port}`;
    statusAddr.textContent = localUrl;
    statusAddr.title = t('proxy.copy');
    statusAddr.onclick = () => {
        navigator.clipboard.writeText(localUrl).then(() => {
            statusAddr.textContent = t('proxy.copied');
            setTimeout(() => { statusAddr.textContent = localUrl; }, 1500);
        }).catch(() => {});
    };

    // LAN addresses — only show when bind is 0.0.0.0
    if (data.host === '0.0.0.0') {
        const ips = (data.local_ips || []).filter(ip => ip !== '127.0.0.1');
        if (ips.length > 0) {
            statusLan.innerHTML = ' / ' + ips.map(ip => {
                const url = `http://${ip}:${data.port}`;
                return `<span class="config-addr clickable" title="${t('proxy.copy')}">${url}</span>`;
            }).join(' / ');
            statusLan.style.display = '';
            // Make each span clickable to copy
            statusLan.querySelectorAll('.clickable').forEach(el => {
                const url = el.textContent;
                el.onclick = () => {
                    navigator.clipboard.writeText(url).then(() => {
                        const orig = el.textContent;
                        el.textContent = t('proxy.copied');
                        setTimeout(() => { el.textContent = orig; }, 1500);
                    }).catch(() => {});
                };
            });
        } else {
            statusLan.style.display = 'none';
        }
    } else {
        statusLan.style.display = 'none';
    }

    // Port settings (hot-reload aware)
    const clampPort = (v, def) => { const n = parseInt(v); return (isNaN(n) || n<1 || n>65535)? def : n; };
    document.getElementById('cfg-port').value = data.port;
    const webPortEl = document.getElementById('cfg-web-port');
    if (webPortEl) {
        webPortEl.value = data.web_port || 8082;
        // Hint visibility
        const hint = document.getElementById('web-port-hint');
        if (hint) hint.style.display = ''; // always show hint per Phase3 spec (hot-restart ~1s)
    }
    document.getElementById('cfg-bind-address').value = data.host || '0.0.0.0';
    document.getElementById('cfg-routing').value = data.routing || 'round-robin';
    document.getElementById('cfg-disable-mapping').checked = data.disable_mapping || false;
    // Upstream models empty → banner (Phase3)
    if (!data.models || Object.keys(data.models).length === 0) {
        const saveSt = document.getElementById('config-save-status');
        if (saveSt) {
            saveSt.textContent = '⚠️ Modèles distants inaccessibles (curl timeout) — vérifiez upstream';
            saveSt.className = 'save-status error';
            setTimeout(()=> saveSt.textContent='', 6000);
        }
        console.warn('[config] upstream models empty — possible _fetch_upstream_models timeout');
    }

    // Debug mode state
    fetchDebugStatus().then(debugData => {
        const debugCb = document.getElementById('cfg-debug-mode');
        const debugText = document.getElementById('debug-status-text');
        const debugLogsTabBtn = document.querySelector('[data-tab="debug-logs"]');
        if (debugCb) debugCb.checked = debugData.enabled;
        if (debugText) {
            debugText.textContent = debugData.enabled ? t('config.debug_enabled') : t('config.debug_off');
            debugText.className = '';
            debugText.style.color = debugData.enabled ? 'var(--success)' : 'var(--text-dim)';
        }
        // Show/hide debug logs tab
        if (debugLogsTabBtn) debugLogsTabBtn.style.display = debugData.enabled ? '' : 'none';
    });

    // Enable/disable mapping fields based on checkbox
    const mappingGrid = document.getElementById('mapping-grid');
    const routeFields = mappingGrid ? mappingGrid.querySelectorAll('.config-field') : [];
    routeFields.forEach(f => f.style.opacity = data.disable_mapping ? '0.4' : '1');

    // Model mapping selects
    const modelIds = Object.keys(data.models);
    availableModels = modelIds;
    for (const route of ['opus', 'sonnet', 'haiku']) {
        const select = document.getElementById(`route-${route}`);
        const currentModel = data.routes[route]?.model || '';
        select.innerHTML = modelIds.map(id =>
            `<option value="${escHtml(id)}" ${id === currentModel ? 'selected' : ''}>${escHtml(id)}</option>`
        ).join('');
    }

    // Populate cr-model select in add section
    const crModelSelect = document.getElementById('cr-model');
    crModelSelect.innerHTML = '<option value="">' + t('config.cr_select_model') + '</option>' +
        modelIds.map(id => `<option value="${escHtml(id)}">${escHtml(id)}</option>`).join('');

    // Available models table
    const tbody = document.getElementById('models-tbody');
    const limits = data.model_limits || {};
    const caps = data.model_capabilities || {};
    const capLabels = { chat: t('cap.chat'), vision: t('cap.vision'), tools: t('cap.tools'), code: t('cap.code'), 'web-search': t('cap.web_search') };
    let html = '';
    for (const [id, info] of Object.entries(data.models)) {
        const lim = limits[id] || [];
        const modelCaps = caps[id] || [];
        const capHtml = modelCaps.map(c => `<span class="cap-badge">${capLabels[c] || c}</span>`).join('') || '<span class="text-dim">-</span>';
        const badge = info.source === 'upstream' ? ' <span class="new-badge">' + t('config.new_badge') + '</span>' : '';
        html += `<tr>
            <td><span class="clickable-model-id" data-id="${escHtml(id)}">${escHtml(id)}</span>${badge}</td>
            <td style="white-space:nowrap">${capHtml}</td>
            <td>${escHtml(info.protocol)}</td>
            <td>${escHtml(info.endpoint)}</td>
            <td>${lim[0] ? formatNumber(lim[0]) : '-'}</td>
            <td>${lim[1] ? formatNumber(lim[1]) : '-'}</td>
            <td>${lim[2] ? formatNumber(lim[2]) : '-'}</td>
        </tr>`;
    }
    tbody.innerHTML = html || '<tr><td colspan="7">' + t('config.no_models') + '</td></tr>';

    // API keys table
    renderApiKeysTable(data.api_keys || []);

    // Custom routes table
    renderCustomRoutes(data.custom_routes || {}, modelIds);

    // Tool routes table — fetch and render
    fetchToolRoutes().then(tools => renderToolRoutes(tools, modelIds));

    // Tool compatibility matrix — fetch and render
    fetchToolCapabilities().then(data => renderToolCompatMatrix(data, modelIds));

    // Apply server-side language if different
    if (data.lang && data.lang !== getLang()) {
        setLang(data.lang);
        const ls = document.getElementById('lang-select');
        if (ls) ls.value = data.lang;
    }
}

function renderCustomRoutes(routes, modelIds) {
    const tbody = document.getElementById('custom-routes-tbody');
    const entries = Object.entries(routes);
    if (entries.length === 0) {
        tbody.innerHTML = '<tr><td colspan="6">' + t('config.cr_no_routes') + '</td></tr>';
        return;
    }
    const opts = modelIds.map(id => `<option value="${id}">${id}</option>`).join('');
    const thinkOpts = ['auto', 'disabled', 'adaptive'].map(v =>
        `<option value="${v}">${t('config.cr_thinking_' + v)}</option>`).join('');
    const effortOpts = ['auto', 'low', 'medium', 'high'].map(v =>
        `<option value="${v}">${t('config.cr_effort_' + v)}</option>`).join('');
    let html = '';
    for (const [key, info] of entries) {
        const match = (info.match || []).join(', ');
        const model = info.model || '';
        const checked = info.enabled !== false ? 'checked' : '';
        const thinkVal = info.thinking || 'auto';
        const effortVal = info.effort || 'auto';
        html += `<tr>
            <td><input type="text" class="config-input cr-edit-match" value="${match}" style="width:100%"></td>
            <td><select class="config-select cr-edit-model" style="width:100%"><option value="">${t('config.cr_select_model')}</option>${opts.replace(`value="${model}"`, `value="${model}" selected`)}</select></td>
            <td style="text-align:center"><input type="checkbox" class="cr-edit-enabled" ${checked}></td>
            <td><select class="config-select cr-edit-thinking" style="width:100%">${thinkOpts.replace(`value="${thinkVal}"`, `value="${thinkVal}" selected`)}</select></td>
            <td><select class="config-select cr-edit-effort" style="width:100%">${effortOpts.replace(`value="${effortVal}"`, `value="${effortVal}" selected`)}</select></td>
            <td><button class="btn btn-danger btn-sm cr-delete-btn" data-key="${key}">${t('config.ak_delete')}</button></td>
        </tr>`;
    }
    tbody.innerHTML = html;
}

function renderToolRoutes(tools, modelIds) {
    const tbody = document.getElementById('tool-routes-tbody');
    if (!tools || tools.length === 0) {
        tbody.innerHTML = '<tr><td colspan="4">' + t('config.tr_no_tools') + '</td></tr>';
        return;
    }
    const opts = modelIds.map(id => `<option value="${id}">${id}</option>`).join('');
    let html = '';
    for (const tool of tools) {
        const selectedRouted = tool.routed_to || '';
        const isRouted = !!selectedRouted;
        html += `<tr>
            <td><code>${escHtml(tool.name)}</code></td>
            <td>${tool.count}</td>
            <td><select class="config-select tr-model-select" style="width:100%" data-tool="${escHtml(tool.name)}">
                <option value="">—</option>
                ${opts.replace(`value="${selectedRouted}"`, `value="${selectedRouted}" selected`)}
            </select></td>
            <td>${isRouted ? '<span class="status-ok">' + t('config.tr_routed') + '</span>' : '<span class="text-dim">' + t('config.tr_unrouted') + '</span>'}</td>
        </tr>`;
    }
    tbody.innerHTML = html;
}

function gatherToolRoutes() {
    const rows = document.querySelectorAll('#tool-routes-tbody tr');
    const toolRoutes = {};
    for (const row of rows) {
        const select = row.querySelector('.tr-model-select');
        if (!select) continue;
        const toolName = select.dataset.tool;
        const model = select.value;
        if (toolName && model) {
            const key = makeRouteKey(toolName);
            toolRoutes[key] = { match: [toolName], model: model };
        }
    }
    return toolRoutes;
}

// ── Tool Compatibility Matrix ──

let _toolCompatData = null;

async function fetchToolCapabilities() {
    try {
        return await apiFetch('/api/config/tool-capabilities');
    } catch (e) {
        console.error('Failed to fetch tool capabilities:', e);
        return null;
    }
}

function renderToolCompatMatrix(data, modelIds) {
    const tbody = document.getElementById('tool-compat-tbody');
    if (!data || !data.all_tools || data.all_tools.length === 0) {
        tbody.innerHTML = '<tr><td colspan="100">No tool data available yet. Make some requests first.</td></tr>';
        return;
    }
    const caps = data.capabilities || {};
    const tools = data.all_tools;
    const models = data.all_models || modelIds;
    const protocols = {};
    // Build protocol map from MODELS config or default
    for (const m of models) {
        protocols[m] = 'openai'; // default
    }

    // Header row with tool names
    let headerHtml = '<tr><th style="min-width:140px">Model</th><th>Protocol</th>';
    for (const tool of tools) {
        headerHtml += `<th style="writing-mode:vertical-lr;text-orientation:mixed;font-size:0.75em;min-width:36px">${escHtml(tool)}</th>`;
    }
    headerHtml += '<th>System Hint</th></tr>';

    // Data rows
    let bodyHtml = '';
    for (const model of models) {
        if (model === '_default' || model === '_doc') continue;
        const cfg = caps[model] || {};
        const defaults = caps['_default'] || {};
        const supported = cfg.supported_tools || defaults.supported_tools;
        const unsupported = cfg.unsupported_tools || defaults.unsupported_tools || [];
        const hint = cfg.system_hint || defaults.system_hint || '';

        bodyHtml += '<tr>';
        bodyHtml += `<td><code style="font-size:0.85em">${escHtml(model)}</code></td>`;
        bodyHtml += `<td class="text-dim" style="font-size:0.8em">${protocols[model] || 'openai'}</td>`;

        for (const tool of tools) {
            let supported_val;
            if (supported !== null && supported !== undefined) {
                supported_val = supported.includes(tool);
            } else {
                supported_val = !unsupported.includes(tool);
            }
            const cls = supported_val ? 'tc-supported' : 'tc-unsupported';
            const icon = supported_val ? '✓' : '✗';
            bodyHtml += `<td class="${cls}" style="text-align:center;cursor:pointer" data-model="${escHtml(model)}" data-tool="${escHtml(tool)}" onclick="toggleToolCompat(this)">${icon}</td>`;
        }

        const hintIcon = hint ? '💡' : '—';
        bodyHtml += `<td style="text-align:center;font-size:0.9em" title="${escHtml(hint)}">${hintIcon}</td>`;
        bodyHtml += '</tr>';
    }

    // Default row
    if (caps['_default']) {
        const def = caps['_default'];
        bodyHtml += '<tr style="border-top:2px solid var(--border)">';
        bodyHtml += '<td><code style="font-size:0.85em">_default</code></td>';
        bodyHtml += '<td class="text-dim" style="font-size:0.8em">—</td>';
        for (const tool of tools) {
            const supported_val = def.unsupported_tools ? !def.unsupported_tools.includes(tool) : true;
            const cls = supported_val ? 'tc-supported' : 'tc-unsupported';
            const icon = supported_val ? '✓' : '✗';
            bodyHtml += `<td class="${cls}" style="text-align:center;cursor:pointer" data-model="_default" data-tool="${escHtml(tool)}" onclick="toggleToolCompat(this)">${icon}</td>`;
        }
        bodyHtml += '<td style="text-align:center;font-size:0.9em">—</td>';
        bodyHtml += '</tr>';
    }

    tbody.innerHTML = headerHtml + bodyHtml;
    _toolCompatData = data;
}

function toggleToolCompat(td) {
    const model = td.dataset.model;
    const tool = td.dataset.tool;
    const isCurrentlySupported = td.textContent.trim() === '✓';
    td.textContent = isCurrentlySupported ? '✗' : '✓';
    td.className = isCurrentlySupported ? 'tc-unsupported' : 'tc-supported';

    // Update _toolCompatData
    if (_toolCompatData && _toolCompatData.capabilities) {
        const caps = _toolCompatData.capabilities;
        const defaults = caps['_default'] || {};
        const modelCfg = caps[model] || {};

        // Switch between whitelist and blacklist modes
        if (isCurrentlySupported) {
            // Was supported, now unsupported — add to unsupported_tools
            const unsupported = modelCfg.unsupported_tools || [];
            if (!unsupported.includes(tool)) {
                unsupported.push(tool);
            }
            caps[model] = { ...modelCfg, unsupported_tools: unsupported };
        } else {
            // Was unsupported, now supported — remove from unsupported_tools
            let unsupported = modelCfg.unsupported_tools || [];
            unsupported = unsupported.filter(t => t !== tool);
            if (unsupported.length === 0 && !modelCfg.supported_tools) {
                delete caps[model].unsupported_tools;
            } else {
                caps[model] = { ...modelCfg, unsupported_tools: unsupported };
            }
        }
    }
}

async function saveToolCapabilities() {
    if (!_toolCompatData || !_toolCompatData.capabilities) return;
    const status = document.getElementById('tc-save-status');
    try {
        await apiFetch('/api/config/tool-capabilities', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify(_toolCompatData.capabilities),
        });
        status.textContent = 'Saved!';
        status.className = 'save-status success';
        setTimeout(() => { status.textContent = ''; }, 3000);
    } catch (e) {
        status.textContent = 'Error saving';
        status.className = 'save-status error';
        console.error(e);
    }
}

// ── Web Search config ──
async function fetchWebSearchConfig() {
    try {
        const data = await apiFetch('/api/config/web-search');
        const modeSelect = document.getElementById('ws-mode-select');
        const modelSelect = document.getElementById('ws-model-select');
        const maxResults = document.getElementById('ws-max-results');

        modeSelect.value = data.mode || 'duckduckgo';
        maxResults.value = data.max_results || 5;

        // Populate model dropdown
        modelSelect.innerHTML = '<option value="">-- select model --</option>';
        (data.available_models || []).forEach(m => {
            const opt = document.createElement('option');
            opt.value = m;
            opt.textContent = m;
            if (m === data.target_model) opt.selected = true;
            modelSelect.appendChild(opt);
        });

        // Show/hide model selector based on mode
        const showModel = data.mode !== 'duckduckgo';
        document.getElementById('ws-model-group').style.display = showModel ? '' : 'none';
    } catch (e) {
        console.error('Failed to fetch web search config:', e);
    }
}

async function saveWebSearchConfig() {
    const status = document.getElementById('ws-save-status');
    try {
        const mode = document.getElementById('ws-mode-select').value;
        const target_model = document.getElementById('ws-model-select').value || null;
        const max_results = parseInt(document.getElementById('ws-max-results').value) || 5;

        await apiFetch('/api/config/web-search', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ mode, target_model, max_results }),
        });
        status.textContent = 'Saved!';
        status.className = 'save-status success';
        setTimeout(() => { status.textContent = ''; }, 3000);
    } catch (e) {
        status.textContent = 'Error saving';
        status.className = 'save-status error';
        console.error(e);
    }
}

function renderApiKeysTable(apiKeys) {
    const tbody = document.getElementById('api-keys-tbody');
    if (!apiKeys || apiKeys.length === 0) {
        tbody.innerHTML = '<tr><td colspan="6">' + t('config.ak_no_keys') + '</td></tr>';
        return;
    }
    tbody.innerHTML = apiKeys.map((k, i) => {
        const keyPlaceholder = k.api_key_masked || '****';
        const ws = k.go_workspace_id || '';
        const wsPlaceholder = k.go_workspace_id_masked || ws.slice(0, 4) + '****' || '';
        const cookiePlaceholder = k.go_auth_cookie_masked || '****';
        return `<tr data-index="${i}">
            <td><input type="text" class="config-input ak-alias" value="${k.alias || ''}" placeholder="${t('config.ak_alias')}" style="width:100%"></td>
            <td><div class="api-key-row"><input type="password" class="config-input ak-key" value="${k.api_key || ''}" placeholder="${keyPlaceholder}" style="width:100%;font-family:monospace"><button class="btn btn-sm btn-toggle-secret" title="${t('show_hide')}">&#128065;</button></div></td>
            <td><input type="text" class="config-input ak-workspace" value="${ws}" style="width:100%"></td>
            <td><div class="api-key-row"><input type="password" class="config-input ak-cookie" value="${k.go_auth_cookie || ''}" placeholder="${cookiePlaceholder}" style="width:100%;font-family:monospace"><button class="btn btn-sm btn-toggle-secret" title="${t('show_hide')}">&#128065;</button></div></td>
            <td style="text-align:center"><input type="checkbox" class="ak-enabled" ${k.enabled !== false ? 'checked' : ''}></td>
            <td><button class="btn btn-danger btn-sm ak-delete-btn">${t('config.ak_delete')}</button></td>
        </tr>`;
    }).join('');
}

function updatePagination(current, total) {
    const pageInfo = document.getElementById('page-info');
    const prevBtn = document.getElementById('prev-page');
    const nextBtn = document.getElementById('next-page');

    pageInfo.textContent = t('page.of').replace('{c}', current).replace('{t}', total);
    prevBtn.disabled = current <= 1;
    nextBtn.disabled = current >= total;
}

function setupTabs() {
    const tabs = document.querySelectorAll('.tab');
    const contents = document.querySelectorAll('.tab-content');
    tabs.forEach(tab => {
        tab.addEventListener('click', () => {
            const target = tab.dataset.tab;
            tabs.forEach(t => t.classList.remove('active'));
            contents.forEach(c => c.classList.remove('active'));
            tab.classList.add('active');
            document.getElementById(target).classList.add('active');
            // Load data when switching tabs
            if (target === 'config') {
                fetchConfig().then(renderConfig);
            } else if (target === 'quotas') {
                fetchQuotas().then(renderQuotas);
            } else if (target === 'vpn') {
                refreshVPNStatus();
            } else if (target === 'debug-logs') {
                debugLogPage = 1;
                fetchDebugLogs(1).then(renderDebugLogLines);
            }
        });
    });
}

function setupFilter() {
    const rangeBtns = document.querySelectorAll('.filter-bar .btn[data-range]');
    const customFields = [document.getElementById('from-date'), document.getElementById('to-date'),
                          document.getElementById('date-sep'), document.getElementById('apply-filter')];

    function setActiveRange(range) {
        rangeBtns.forEach(b => b.classList.toggle('active', b.dataset.range === range));
        const isCustom = range === 'custom';
        customFields.forEach(el => el.style.display = isCustom ? '' : 'none');
    }

    rangeBtns.forEach(btn => {
        btn.addEventListener('click', () => {
            const range = btn.dataset.range;
            setActiveRange(range);
            currentPage = 1; // Reset to first page when filter changes
            if (range === 'today') { filterFrom = todayStr(); filterTo = todayStr(); }
            else if (range === '7d') { filterFrom = daysAgoStr(7); filterTo = todayStr(); }
            else if (range === '30d') { filterFrom = daysAgoStr(30); filterTo = todayStr(); }
            else if (range === 'custom') {
                document.getElementById('from-date').value = filterFrom;
                document.getElementById('to-date').value = filterTo;
                return;
            }
            refreshAll();
        });
    });

    document.getElementById('apply-filter').addEventListener('click', () => {
        filterFrom = document.getElementById('from-date').value;
        filterTo = document.getElementById('to-date').value;
        currentPage = 1;
        refreshAll();
    });
}

// ── History Filters ──

async function fetchHistoryFilters() {
    try {
        const data = await apiFetch('/api/history/filters');
        populateFilterDropdown('filter-model', data.models || []);
        populateFilterDropdown('filter-original-model', data.original_models || []);
        populateFilterDropdown('filter-account', data.accounts || []);
        populateFilterDropdown('filter-tool', data.tools_used || []);
    } catch (e) {
        console.error('Failed to fetch history filters:', e);
    }
}

function populateFilterDropdown(id, values) {
    const select = document.getElementById(id);
    if (!select) return;
    const current = select.value;
    select.innerHTML = '<option value="">All</option>';
    values.forEach(v => {
        if (!v) return;
        const opt = document.createElement('option');
        opt.value = v;
        opt.textContent = v;
        if (v === current) opt.selected = true;
        select.appendChild(opt);
    });
}


function setupHistoryFilters() {
    const applyBtn = document.getElementById('filter-apply-btn');
    const resetBtn = document.getElementById('filter-reset-btn');

    if (applyBtn) {
        applyBtn.addEventListener('click', () => {
            filterStatus = document.getElementById('filter-status').value;
            filterModel = document.getElementById('filter-model').value;
            filterOriginalModel = document.getElementById('filter-original-model').value;
            filterAccount = document.getElementById('filter-account').value;
            filterTool = document.getElementById('filter-tool').value;
            filterStream = document.getElementById('filter-stream').value;
            filterSearch = document.getElementById('filter-search').value;
            currentPage = 1;
            refreshAll();
        });
    }

    if (resetBtn) {
        resetBtn.addEventListener('click', () => {
            document.getElementById('filter-status').value = '';
            document.getElementById('filter-model').value = '';
            document.getElementById('filter-original-model').value = '';
            document.getElementById('filter-account').value = '';
            document.getElementById('filter-tool').value = '';
            const _fs = document.getElementById('filter-stream');
            if (_fs) _fs.value = '';
            document.getElementById('filter-search').value = '';
            filterStatus = '';
            filterModel = '';
            filterOriginalModel = '';
            filterAccount = '';
            filterTool = '';
            filterStream = '';
            filterSearch = '';
            currentPage = 1;
            refreshAll();
        });
    }

    // Also apply on Enter in search field
    const searchInput = document.getElementById('filter-search');
    if (searchInput) {
        searchInput.addEventListener('keydown', (e) => {
            if (e.key === 'Enter') {
                applyBtn.click();
            }
        });
    }

    // Fetch filter options
    fetchHistoryFilters();
}

function setupConfig() {
    // Disable mapping toggle
    const disableMapping = document.getElementById('cfg-disable-mapping');
    const mappingGrid = document.getElementById('mapping-grid');
    disableMapping.addEventListener('change', () => {
        const mappingFields = mappingGrid ? mappingGrid.querySelectorAll('.config-field, .config-select') : [];
        mappingFields.forEach(f => {
            f.disabled = disableMapping.checked;
            f.style.opacity = disableMapping.checked ? '0.4' : '1';
        });
    });

    // Bind address change → toggle LAN address visibility
    document.getElementById('cfg-bind-address').addEventListener('change', function () {
        const lanEl = document.getElementById('proxy-status-lan');
        if (!lanEl) return;
        if (this.value === '0.0.0.0') {
            // Re-render to show LAN addr if available
            fetchConfig().then(renderConfig);
        } else {
            lanEl.style.display = 'none';
        }
    });

    // Save config (hot-reload aware, Phase3: web_port hot-restart, no full_restart)
    const saveBtn = document.getElementById('btn-save-config');
    const saveStatus = document.getElementById('config-save-status');
    saveBtn.addEventListener('click', async () => {
        const clampPort = (elId, def) => {
            const v = parseInt(document.getElementById(elId)?.value);
            if (isNaN(v) || v<1 || v>65535) {
                const el = document.getElementById(elId);
                if (el) { el.style.borderColor='var(--danger)'; setTimeout(()=> el.style.borderColor='',2000); }
                return null;
            }
            return v;
        };
        const port = clampPort('cfg-port', 4000);
        const webPort = clampPort('cfg-web-port', 8082);
        if (port===null || webPort===null) {
            saveStatus.textContent = 'Port invalide (1-65535)';
            saveStatus.className='save-status error';
            return;
        }
        const payload = {
            routes: {
                opus: document.getElementById('route-opus').value,
                sonnet: document.getElementById('route-sonnet').value,
                haiku: document.getElementById('route-haiku').value,
            },
            port: port,
            web_port: webPort,
            host: document.getElementById('cfg-bind-address').value,
            routing: document.getElementById('cfg-routing').value,
            disable_mapping: document.getElementById('cfg-disable-mapping').checked,
        };
        const prevPort = configData?.port;
        const prevWebPort = configData?.web_port;
        const prevHost = configData?.host;
        const willRestart = (port!==prevPort || webPort!==prevWebPort || payload.host!==prevHost);
        if (willRestart) {
            const notice = document.getElementById('restart-notice');
            if (notice) { notice.textContent = 'Redémarrage gracieux en cours… (~1s)'; notice.style.display=''; }
            saveStatus.textContent = 'Redémarrage gracieux…';
            saveStatus.className='save-status';
        }
        try {
            const result = await apiFetch('/api/config', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify(payload),
            });
            // Phase3: never needs_restart true for GUI — hot-restart is transient
            const notice = document.getElementById('restart-notice');
            if (notice) {
                if (result.needs_restart) {
                    notice.textContent = t('proxy.restart_notice');
                    notice.style.display='';
                } else {
                    // hide after brief toast if we showed hot-restart
                    if (willRestart) setTimeout(()=> notice.style.display='none', 4000);
                    else notice.style.display='none';
                }
            }
            if (result.status==='error') throw new Error(result.message);
            saveStatus.textContent = result.message || t('config.saved');
            saveStatus.className = 'save-status success';
            setTimeout(() => { saveStatus.textContent = ''; }, 3000);
            // If hot-restart happened, wait a bit then refresh (server may be briefly down)
            if (willRestart) {
                await new Promise(r=> setTimeout(r, 1500));
                // Try new web_port if changed
                if (webPort!==prevWebPort) {
                    // attempt to fetch via new port — but we stay on same origin, so just retry
                    let retries=3;
                    while(retries-->0){
                        try{ await fetchConfig(); break; }catch(e){ await new Promise(r=>setTimeout(r,800)); }
                    }
                }
            }
            fetchConfig().then(renderConfig);
        } catch (e) {
            const notice = document.getElementById('restart-notice');
            if (notice) notice.style.display='none';
            saveStatus.textContent = e.message || 'Error saving';
            saveStatus.className = 'save-status error';
            console.error('Failed to save config:', e);
        }
    });

    // Proxy start/stop
    document.getElementById('btn-proxy-start').addEventListener('click', async () => {
        try {
            await apiFetch('/api/proxy/start', { method: 'POST' }).catch(() => {});
            fetchConfig().then(renderConfig);
        } catch (e) { console.error(e); }
    });

    document.getElementById('btn-proxy-stop').addEventListener('click', async () => {
        try {
            await apiFetch('/api/proxy/stop', { method: 'POST' }).catch(() => {});
            fetchConfig().then(renderConfig);
        } catch (e) { console.error(e); }
    });

    document.getElementById('btn-proxy-restart').addEventListener('click', async () => {
        try {
            await apiFetch('/api/proxy/restart', { method: 'POST' }).catch(() => {});
            await new Promise(r => setTimeout(r, 1500));
            fetchConfig().then(renderConfig);
        } catch (e) { console.error(e); }
    });

    document.getElementById('btn-proxy-full-restart').addEventListener('click', async () => {
        if (!confirm(t('proxy.full_restart_confirm'))) return;
        try {
            await apiFetch('/api/proxy/restart?full=true', { method: 'POST' }).catch(() => {});
            // Wait for the process to fully restart
            await new Promise(r => setTimeout(r, 3000));
            fetchConfig().then(renderConfig);
        } catch (e) { console.error(e); }
    });

    // Custom routes: add
    document.getElementById('cr-add-btn').addEventListener('click', () => {
        const match = document.getElementById('cr-match').value.trim();
        const model = document.getElementById('cr-model').value;
        if (!match || !model) { alert(t('config.cr_alert')); return; }
        const tbody = document.getElementById('custom-routes-tbody');
        const key = makeRouteKey(match);
        const opts = availableModels.map(id => `<option value="${id}" ${id === model ? 'selected' : ''}>${id}</option>`).join('');
        const row = document.createElement('tr');
        row.id = `cr-row-${key}`;
        const thinkOptsA = ['auto', 'disabled', 'adaptive'].map(v =>
            `<option value="${v}">${t('config.cr_thinking_' + v)}</option>`).join('');
        const effortOptsA = ['auto', 'low', 'medium', 'high'].map(v =>
            `<option value="${v}">${t('config.cr_effort_' + v)}</option>`).join('');
        const thinking = document.getElementById('cr-thinking').value;
        const effort = document.getElementById('cr-effort').value;
        row.innerHTML = `<td><input type="text" class="config-input cr-edit-match" value="${match}" style="width:100%"></td>
            <td><select class="config-select cr-edit-model" style="width:100%"><option value="">${t('config.cr_select_model')}</option>${opts}</select></td>
            <td style="text-align:center"><input type="checkbox" class="cr-edit-enabled" checked></td>
            <td><select class="config-select cr-edit-thinking" style="width:100%">${thinkOptsA.replace(`value="${thinking}"`, `value="${thinking}" selected`)}</select></td>
            <td><select class="config-select cr-edit-effort" style="width:100%">${effortOptsA.replace(`value="${effort}"`, `value="${effort}" selected`)}</select></td>
            <td><button class="btn btn-danger btn-sm cr-delete-btn" data-key="${key}">${t('config.ak_delete')}</button></td>`;
        // Remove empty state
        if (tbody.querySelector('td[colspan]')) tbody.innerHTML = '';
        tbody.appendChild(row);
        document.getElementById('cr-match').value = '';
        document.getElementById('cr-model').selectedIndex = 0;
    });

    // Custom routes: delete (delegated)
    document.getElementById('custom-routes-tbody').addEventListener('click', (e) => {
        const btn = e.target.closest('.cr-delete-btn');
        if (!btn) return;
        const row = btn.closest('tr');
        if (row) row.remove();
        const tbody = document.getElementById('custom-routes-tbody');
        if (!tbody.querySelector('tr')) {
            tbody.innerHTML = '<tr><td colspan="6">' + t('config.cr_no_routes') + '</td></tr>';
        }
    });

    // Custom routes: save
    document.getElementById('cr-save-btn').addEventListener('click', async () => {
        const tbody = document.getElementById('custom-routes-tbody');
        const rows = tbody.querySelectorAll('tr');
        const routes = {};
        for (const row of rows) {
            const matchInput = row.querySelector('.cr-edit-match');
            const modelInput = row.querySelector('.cr-edit-model');
            if (!matchInput || !modelInput) continue;
            const match = matchInput.value.trim();
            const model = modelInput.value.trim();
            if (!match || !model) continue;
            const key = makeRouteKey(match);
            const routeData = { match: match.split(',').map(s => s.trim()), model: model };
            const enabled = row.querySelector('.cr-edit-enabled')?.checked ?? true;
            if (!enabled) routeData.enabled = false;
            const thinking = row.querySelector('.cr-edit-thinking')?.value;
            if (thinking && thinking !== 'auto') routeData.thinking = thinking;
            const effort = row.querySelector('.cr-edit-effort')?.value;
            if (effort && effort !== 'auto') routeData.effort = effort;
            routes[key] = routeData;
        }
        const status = document.getElementById('cr-save-status');
        try {
            await apiFetch('/api/config/custom-routes', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify(routes),
            });
            status.textContent = t('config.cr_saved');
            status.className = 'save-status success';
            setTimeout(() => { status.textContent = ''; }, 3000);
            fetchConfig().then(renderConfig);
        } catch (e) {
            status.textContent = 'Error saving';
            status.className = 'save-status error';
            console.error(e);
        }
    });

    // ── Tool Routes: toggle universal tools visibility ──
    document.getElementById('tr-show-all').addEventListener('change', async (e) => {
        const tools = await fetchToolRoutes(7, e.target.checked);
        renderToolRoutes(tools, availableModels);
    });

    // ── Tool Routes: save ──
    document.getElementById('tr-save-btn').addEventListener('click', async () => {
        const toolRoutes = gatherToolRoutes();

        // Fetch existing custom routes and merge
        const configData = await apiFetch('/api/config');
        const existingRoutes = configData.custom_routes || {};

        // Remove old tool routes (those matching known tool names from the current data)
        const currentTools = await fetchToolRoutes(7, true); // fetch all including universal
        const currentToolNames = new Set(currentTools.map(t => t.name));
        for (const key of Object.keys(existingRoutes)) {
            const matches = existingRoutes[key].match || [];
            if (matches.some(m => currentToolNames.has(m))) {
                delete existingRoutes[key];
            }
        }

        // Merge new tool routes
        const mergedRoutes = { ...existingRoutes, ...toolRoutes };

        const status = document.getElementById('tr-save-status');
        try {
            await apiFetch('/api/config/custom-routes', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify(mergedRoutes),
            });
            status.textContent = t('config.tr_saved');
            status.className = 'save-status success';
            setTimeout(() => { status.textContent = ''; }, 3000);
            fetchConfig().then(renderConfig);
        } catch (e) {
            status.textContent = 'Error saving';
            status.className = 'save-status error';
            console.error(e);
        }
    });

    // ── Tool Capabilities save ──
    document.getElementById('tc-save-btn').addEventListener('click', saveToolCapabilities);

    // ── Web Search config ──
    fetchWebSearchConfig();
    document.getElementById('ws-save-btn').addEventListener('click', saveWebSearchConfig);
    document.getElementById('ws-mode-select').addEventListener('change', (e) => {
        const showModel = e.target.value !== 'duckduckgo';
        document.getElementById('ws-model-group').style.display = showModel ? '' : 'none';
    });

    // ── API Keys management ──

    // Add key row
    document.getElementById('api-key-add-btn').addEventListener('click', () => {
        const tbody = document.getElementById('api-keys-tbody');
        const emptyRow = tbody.querySelector('td[colspan]');
        if (emptyRow) tbody.innerHTML = '';
        const tr = document.createElement('tr');
        tr.innerHTML = `
            <td><input type="text" class="config-input ak-alias" placeholder="${t('config.ak_alias')}" style="width:100%"></td>
            <td><div class="api-key-row"><input type="password" class="config-input ak-key" placeholder="sk-..." style="width:100%;font-family:monospace"><button class="btn btn-sm btn-toggle-secret" title="${t('show_hide')}">&#128065;</button></div></td>
            <td><input type="text" class="config-input ak-workspace" placeholder="wrk_..." style="width:100%"></td>
            <td><div class="api-key-row"><input type="password" class="config-input ak-cookie" placeholder="Fe26.2..." style="width:100%;font-family:monospace"><button class="btn btn-sm btn-toggle-secret" title="${t('show_hide')}">&#128065;</button></div></td>
            <td style="text-align:center"><input type="checkbox" class="ak-enabled" checked></td>
            <td><button class="btn btn-danger btn-sm ak-delete-btn">${t('config.ak_delete')}</button></td>
        `;
        tbody.appendChild(tr);
    });

    // Delete key row (delegated)
    document.getElementById('api-keys-tbody').addEventListener('click', (e) => {
        const btn = e.target.closest('.ak-delete-btn');
        if (!btn) return;
        const row = btn.closest('tr');
        if (row) row.remove();
        const tbody = document.getElementById('api-keys-tbody');
        if (!tbody.querySelector('tr')) {
            tbody.innerHTML = '<tr><td colspan="6">' + t('config.ak_no_keys') + '</td></tr>';
        }
    });

    // Toggle secret visibility (delegated)
    document.getElementById('api-keys-tbody').addEventListener('click', (e) => {
        const btn = e.target.closest('.btn-toggle-secret');
        if (!btn) return;
        const input = btn.previousElementSibling;
        if (input && input.type === 'password') {
            input.type = 'text';
            btn.textContent = '\u{1F441}';
        } else if (input) {
            input.type = 'password';
            btn.textContent = '\u{1F441}';
        }
    });

    // Save API keys
    document.getElementById('api-key-save-btn').addEventListener('click', async () => {
        const rows = document.querySelectorAll('#api-keys-tbody tr');
        const keys = [];
        for (const row of rows) {
            const apiKey = row.querySelector('.ak-key')?.value?.trim();
            if (!apiKey) continue; // skip empty rows
            keys.push({
                api_key: apiKey,
                alias: row.querySelector('.ak-alias')?.value?.trim() || '',
                go_workspace_id: row.querySelector('.ak-workspace')?.value?.trim() || '',
                go_auth_cookie: row.querySelector('.ak-cookie')?.value?.trim() || '',
                enabled: row.querySelector('.ak-enabled')?.checked ?? true,
            });
        }
        const status = document.getElementById('api-key-save-status');
        try {
            await apiFetch('/api/config/api-keys', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ api_keys: keys }),
            });
            status.textContent = t('config.ak_saved');
            status.className = 'save-status success';
            setTimeout(() => { status.textContent = ''; }, 3000);
            fetchConfig().then(renderConfig);
        } catch (e) {
            status.textContent = 'Error saving';
            status.className = 'save-status error';
            console.error(e);
        }
    });

    // ── Debug mode toggle ──
    const debugCb = document.getElementById('cfg-debug-mode');
    const debugText = document.getElementById('debug-status-text');
    if (debugCb) {
        debugCb.addEventListener('change', async () => {
            const enabled = debugCb.checked;
            const result = await setDebugMode(enabled);
            if (debugText) {
                debugText.textContent = enabled ? t('config.debug_enabled') : t('config.debug_off');
                debugText.className = '';
                debugText.style.color = enabled ? 'var(--success)' : 'var(--text-dim)';
            }
            // Show/hide debug logs tab
            const debugLogsTabBtn = document.querySelector('[data-tab="debug-logs"]');
            if (debugLogsTabBtn) debugLogsTabBtn.style.display = enabled ? '' : 'none';
        });
    }
}

var _refreshing = false;
async function refreshAll() {
    if (_refreshing) return;
    _refreshing = true;
    try {
        const [stats, history, quotas] = await Promise.all([
            fetchStats(filterFrom, filterTo),
            fetchHistory(filterFrom, filterTo, currentPage),
            fetchQuotas()
        ]);
        renderStats(stats);
        renderCharts(stats);
        renderHistory(history);
        renderQuotas(quotas);
        renderTimeSeriesCharts(filterFrom, filterTo);
        document.getElementById('last-update').textContent = t('last.update') + formatTime();
    } finally {
        _refreshing = false;
    }
}

async function loadHistory() {
    const history = await fetchHistory(filterFrom, filterTo, currentPage);
    renderHistory(history);
}

async function deleteHistory(before = null, all = false) {
    try {
        let url = '/api/history?';
        if (all) url += 'all=true';
        else {
            const d = before || filterTo;
            url += `before=${d}`;
        }
        await apiFetch(url, { method: 'DELETE' });
        currentPage = 1;
        refreshAll();
    } catch (e) {
        console.error('Failed to delete:', e);
    }
}

document.addEventListener('DOMContentLoaded', () => {
    // Theme
    const saved = localStorage.getItem('theme') || 'dark';
    document.documentElement.setAttribute('data-theme', saved);
    const toggle = document.getElementById('theme-toggle');
    toggle.textContent = saved === 'dark' ? '☾' : '☀';
    toggle.addEventListener('click', () => {
        const current = document.documentElement.getAttribute('data-theme');
        const next = current === 'dark' ? 'light' : 'dark';
        document.documentElement.setAttribute('data-theme', next);
        localStorage.setItem('theme', next);
        toggle.textContent = next === 'dark' ? '☾' : '☀';
    });

    setupTabs();
    setupFilter();
    setupHistoryFilters();
    setupConfig();
    // Load config first so renderQuotas can map workspace IDs to aliases
    fetchConfig().then(() => refreshAll());

    // Show/hide debug logs tab based on debug mode state
    fetchDebugStatus().then(debugData => {
        const debugLogsTabBtn = document.querySelector('[data-tab="debug-logs"]');
        if (debugLogsTabBtn) debugLogsTabBtn.style.display = debugData.enabled ? '' : 'none';
    });

    // Language selector
    const langSelect = document.getElementById('lang-select');
    if (langSelect) {
        langSelect.value = getLang();
        langSelect.addEventListener('change', () => {
            const newLang = langSelect.value;
            setLang(newLang);
            apiFetch('/api/config', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ lang: newLang })
            }).catch(() => {});
        });
    }

    // Apply initial language to static elements
    document.querySelectorAll('[data-i18n]').forEach(el => {
        el.textContent = t(el.dataset.i18n);
    });

    // Fetch server-side language preference (may override localStorage)
    apiFetch('/api/config').then(data => {
        if (data.lang && data.lang !== getLang()) {
            setLang(data.lang);
            if (langSelect) langSelect.value = data.lang;
        }
    }).catch(() => {});

    // ── SSE real-time updates ──
    let eventSource = null;
    let pollTimer = null;
    let sseRetryDelay = 1000;
    const SSE_MAX_DELAY = 30000;

    function startPolling(intervalMs) {
        if (pollTimer) clearInterval(pollTimer);
        pollTimer = setInterval(refreshAll, intervalMs);
    }

    function stopPolling() {
        if (pollTimer) { clearInterval(pollTimer); pollTimer = null; }
    }

    function connectSSE() {
        if (window._sseRetry) { clearTimeout(window._sseRetry); window._sseRetry = null; }
        if (eventSource) eventSource.close();
        const es = new EventSource('/api/events');
        eventSource = es;

        es.addEventListener('stats_updated', () => {
            // Debounce: coalesce rapid events into a single refresh
            if (window._sseRefresh) clearTimeout(window._sseRefresh);
            window._sseRefresh = setTimeout(() => {
                window._sseRefresh = null;
                refreshAll();
            }, 200);
        });
        es.addEventListener('connected', () => { stopPolling(); sseRetryDelay = 1000; });
        es.addEventListener('quotas_updated', () => {
            fetchQuotas().then(renderQuotas);
        });
        es.addEventListener('models_updated', () => {
            fetchConfig().then(data => {
                if (data) { configData = data; renderConfig(data); }
            });
        });

        es.onerror = () => {
            if (es !== eventSource) return; // stale
            if (eventSource) { eventSource.close(); eventSource = null; }
            if (!pollTimer) startPolling(15000);
            if (!window._sseRetry) {
                window._sseRetry = setTimeout(() => {
                    window._sseRetry = null;
                    connectSSE();
                }, sseRetryDelay);
                sseRetryDelay = Math.min(sseRetryDelay * 2, SSE_MAX_DELAY);
            }
        };
    }

    connectSSE();

    // Backup poll in case SSE never connects
    if (!pollTimer) startPolling(30000);

    document.addEventListener('visibilitychange', () => {
        if (document.hidden) {
            if (eventSource) { eventSource.close(); eventSource = null; }
            stopPolling();
        } else {
            refreshAll();
            if (!eventSource) connectSSE();
            if (!pollTimer) startPolling(30000);
        }
    });

    // ── Quota countdown ticker ──
    setInterval(() => {
        document.querySelectorAll('.quota-reset').forEach(el => {
            const target = parseInt(el.dataset.resetTarget || '0', 10);
            if (!target) return;
            const remaining = target - Math.floor(Date.now() / 1000);
            el.textContent = remaining <= 0 ? t('quotas.resetting') : t('quotas.resets_in') + formatResetTime(remaining);
        });
    }, 1000);

    const deleteBtn = document.getElementById('delete-btn');
    const deleteMenu = document.getElementById('delete-menu');
    const deleteDate = document.getElementById('delete-date');

    deleteBtn.addEventListener('click', () => {
        deleteMenu.style.display = deleteMenu.style.display === 'none' ? '' : 'none';
    });

    document.getElementById('delete-all-opt').addEventListener('click', () => {
        if (confirm(t('delete.confirm_all'))) {
            deleteHistory(null, true);
        }
        deleteMenu.style.display = 'none';
    });

    document.getElementById('delete-by-date-opt').addEventListener('click', () => {
        deleteMenu.style.display = 'none';
        document.getElementById('delete-date').value = todayStr();
        document.getElementById('delete-modal').style.display = '';
    });

    document.getElementById('modal-delete-btn').addEventListener('click', () => {
        const d = document.getElementById('delete-date').value;
        if (d && confirm(t('delete.confirm_before').replace('{d}', d))) {
            deleteHistory(d);
        }
        document.getElementById('delete-modal').style.display = 'none';
    });

    document.getElementById('modal-cancel-btn').addEventListener('click', () => {
        document.getElementById('delete-modal').style.display = 'none';
    });

    // Pagination buttons
    document.getElementById('prev-page').addEventListener('click', () => {
        if (currentPage > 1) {
            currentPage--;
            loadHistory();
        }
    });

    document.getElementById('next-page').addEventListener('click', () => {
        if (currentPage < totalPages) {
            currentPage++;
            loadHistory();
        }
    });

    // Request detail modal
    document.getElementById('req-detail-close').addEventListener('click', () => {
        document.getElementById('request-detail-modal').style.display = 'none';
    });
    document.getElementById('request-detail-modal').addEventListener('click', (e) => {
        if (e.target === e.currentTarget) {
            e.currentTarget.style.display = 'none';
        }
    });

    // History row click → show detail
    document.getElementById('history-tbody').addEventListener('click', (e) => {
        const row = e.target.closest('tr[data-req-id]');
        if (row) showRequestDetail(row.dataset.reqId);
    });

    document.addEventListener('click', (e) => {
        if (!e.target.closest('.delete-section')) {
            deleteMenu.style.display = 'none';
        }
    });

    // ── Debug Logs: clear ──
    document.getElementById('btn-clear-debug-log').addEventListener('click', async () => {
        const status = document.getElementById('debug-log-status');
        try {
            await clearDebugLogs();
            status.textContent = t('debug.cleared');
            status.className = 'save-status success';
            setTimeout(() => { status.textContent = ''; }, 3000);
            debugLogPage = 1;
            fetchDebugLogs(1).then(renderDebugLogLines);
        } catch (e) {
            status.textContent = 'Error';
            status.className = 'save-status error';
        }
    });

    // ── Debug Logs: pagination ──
    document.getElementById('debug-prev-page').addEventListener('click', () => {
        if (debugLogPage > 1) {
            debugLogPage--;
            fetchDebugLogs(debugLogPage).then(renderDebugLogLines);
        }
    });

    document.getElementById('debug-next-page').addEventListener('click', () => {
        if (debugLogPage < debugLogTotalPages) {
            debugLogPage++;
            fetchDebugLogs(debugLogPage).then(renderDebugLogLines);
        }
    });

    // ── Debug Logs: auto-refresh ──
    let debugAutoRefreshTimer = null;
    document.getElementById('debug-auto-refresh').addEventListener('change', (e) => {
        if (e.target.checked) {
            debugAutoRefreshTimer = setInterval(() => {
                // Only refresh if debug-logs tab is active
                const activeTab = document.querySelector('.tab.active');
                if (activeTab && activeTab.dataset.tab === 'debug-logs') {
                    fetchDebugLogs(debugLogPage).then(renderDebugLogLines);
                }
            }, 3000);
        } else {
            if (debugAutoRefreshTimer) { clearInterval(debugAutoRefreshTimer); debugAutoRefreshTimer = null; }
        }
    });

    // ── Model ID click-to-copy ──
    document.getElementById('models-tbody').addEventListener('click', (e) => {
        const el = e.target.closest('.clickable-model-id');
        if (!el) return;
        const id = el.dataset.id;
        if (!id) return;
        navigator.clipboard.writeText(id).then(() => {
            const orig = el.textContent;
            el.textContent = '✓ ' + t('proxy.copied');
            el.style.color = 'var(--success)';
            setTimeout(() => {
                el.textContent = orig;
                el.style.color = '';
            }, 1500);
        }).catch(() => {});
    });

    // ── VPN tab refresh (parallélisé + état chargement) ──
    let _vpnRefreshInFlight = false;
    async function refreshVPNStatus() {
        if (_vpnRefreshInFlight) return;
        _vpnRefreshInFlight = true;
        const fmToggle = document.getElementById('fm-toggle');
        const statusEl = document.getElementById('vpn-status-text');
        const statusDot = document.getElementById('vpn-status-dot');
        // État chargement : désactive le toggle, affiche "Chargement..." sans perdre enabled
        const prevEnabled = fmToggle ? fmToggle.checked : null;
        if (fmToggle) fmToggle.disabled = true;
        if (statusEl && !statusEl.dataset.loading) {
            statusEl.dataset.orig = statusEl.textContent;
            statusEl.textContent = 'Chargement...';
            statusEl.dataset.loading = '1';
            if (statusDot) statusDot.style.background = 'var(--warning)';
        }
        // Optimiste : affiche dernier état connu depuis localStorage pendant le chargement
        try {
            const cached = localStorage.getItem('vpn_enabled_cache');
            if (cached !== null && fmToggle && prevEnabled === false) {
                const cachedEnabled = cached === '1';
                if (cachedEnabled) {
                    fmToggle.checked = true;
                }
            }
        } catch (e) {}

        const fetchTimeout = (url, ms=4000) => {
            const ctrl = new AbortController();
            const id = setTimeout(()=> ctrl.abort(), ms);
            return fetch(url, {signal: ctrl.signal}).then(r=> {
                clearTimeout(id);
                if (!r.ok) throw new Error('HTTP '+r.status);
                return r.json();
            }).catch(e=> {
                clearTimeout(id);
                throw e;
            });
        };

        // Parallélise les 7 fetches (au lieu de 7 await séquentiels)
        const results = await Promise.allSettled([
            fetchTimeout('/api/vpn-status', 5000),
            fetchTimeout('/api/vpn/credentials', 4000),
            fetchTimeout('/api/vpn-config', 4000),
            fetchTimeout('/api/vpn/socks5', 4000),
            fetchTimeout('/api/config/free-model-map', 4000),
            fetchTimeout('/api/vpn/rotation-rules', 4000),
            fetchTimeout('/api/vpn/schedule', 4000),
        ]);

        const [vpnStatusRes, credRes, cfgRes, socks5Res, freeMapRes, rotationRes, scheduleRes] = results;

        // 1) VPN status
        if (vpnStatusRes.status === 'fulfilled' && vpnStatusRes.value) {
            try { updateVPNUI(vpnStatusRes.value); } catch (e) { console.error('updateVPNUI', e); }
        } else {
            console.warn('vpn-status slow/failed:', vpnStatusRes.reason?.message);
        }

        // 2) Credentials
        if (credRes.status === 'fulfilled' && credRes.value) {
            try {
                const credData = credRes.value;
                const credStatus = document.getElementById('vpn-cred-status');
                const usernameEl = document.getElementById('vpn-cred-username');
                const passwordEl = document.getElementById('vpn-cred-password');
                const fileEl = document.getElementById('vpn-cred-file');
                if (credData.exists) {
                    if (credStatus) credStatus.innerHTML = '<span style="color:var(--success)">&#10003;</span> ' + (t('vpn.credentials_saved') || 'Enregistré');
                    if (usernameEl) usernameEl.textContent = credData.username_preview || '****';
                    if (passwordEl) passwordEl.textContent = '••••••••';
                    if (fileEl) fileEl.textContent = credData.path || 'vpn/credentials.txt';
                } else {
                    if (credStatus) credStatus.innerHTML = '<span style="color:var(--warning)">!</span> ' + (t('vpn.credentials_missing') || 'Aucun identifiant');
                    if (usernameEl) usernameEl.textContent = '—';
                    if (passwordEl) passwordEl.textContent = '—';
                    if (fileEl) fileEl.textContent = '—';
                }
            } catch (e) { console.error('cred UI', e); }
        }

        // 3) VPN config (contient enabled)
        if (cfgRes.status === 'fulfilled' && cfgRes.value) {
            try {
                const cfgData = cfgRes.value;
                renderServerList(cfgData.servers || []);
                if (cfgData.mode) document.getElementById('vpn-mode').value = cfgData.mode;
                if (cfgData.proxy_port) document.getElementById('vpn-proxy-port').value = cfgData.proxy_port;
                if (cfgData.switch_delay) document.getElementById('vpn-switch-delay').value = cfgData.switch_delay;
                if (cfgData.quota_per_ip) document.getElementById('vpn-quota-per-ip').value = cfgData.quota_per_ip;
                if (cfgData.docker_image) {
                    const dockerSelect = document.getElementById('vpn-docker-image');
                    const knownImages = ['qmcgaw/gluetun', 'nordvpn-official'];
                    if (knownImages.includes(cfgData.docker_image)) {
                        dockerSelect.value = cfgData.docker_image;
                    } else {
                        dockerSelect.value = 'custom';
                        const customInput = document.getElementById('vpn-docker-image-custom');
                        if (customInput) {
                            customInput.style.display = 'block';
                            customInput.value = cfgData.docker_image;
                        }
                    }
                    vpnDockerImageChanged();
                }
                vpnInitConfig(cfgData.mode);
                if (cfgData.circuit_breaker_threshold) document.getElementById('vpn-cb-threshold').value = cfgData.circuit_breaker_threshold;
                if (cfgData.circuit_breaker_recovery) document.getElementById('vpn-cb-recovery').value = cfgData.circuit_breaker_recovery;
                if (cfgData.backoff_max_delay) document.getElementById('vpn-backoff-max').value = cfgData.backoff_max_delay;
                if (cfgData.watchdog_interval !== undefined) document.getElementById('vpn-watchdog').value = cfgData.watchdog_interval;
                if (cfgData.api_cache_ttl) document.getElementById('vpn-api-cache').value = cfgData.api_cache_ttl;
                if (cfgData.proxy_mode) setProxyModeUI(cfgData.proxy_mode);
                const fmT = document.getElementById('fm-toggle');
                if (fmT) {
                    fmT.checked = !!cfgData.enabled;
                    try { localStorage.setItem('vpn_enabled_cache', cfgData.enabled ? '1' : '0'); } catch (e) {}
                }
            } catch (e) { console.error('cfg UI', e); }
        } else {
            console.warn('vpn-config slow/failed:', cfgRes.reason?.message);
        }

        // 4) SOCKS5
        if (socks5Res.status === 'fulfilled' && socks5Res.value) {
            try {
                renderSocks5List(socks5Res.value.proxies || []);
                const rotateToggle = document.getElementById('fm-socks5-rotate');
                if (rotateToggle) rotateToggle.checked = socks5Res.value.rotate !== false;
            } catch (e) {}
        }

        // 5) Free Model Map (utilise déjà le résultat parallèle, évite 2e fetch)
        if (freeMapRes.status === 'fulfilled' && freeMapRes.value) {
            try {
                _freeModelMap = freeMapRes.value.free_model_map || {};
                renderFreeModelMap();
                const paidSel = document.getElementById('fmm-paid');
                const freeSel = document.getElementById('fmm-free');
                const models = freeMapRes.value.available_models || availableModels || [];
                if (paidSel) {
                    const curPaid = paidSel.value;
                    paidSel.innerHTML = '<option value="">— choisir —</option>' + models.map(m=> `<option value="${escHtml(m)}">${escHtml(m)}</option>`).join('');
                    if (curPaid) paidSel.value = curPaid;
                }
                if (freeSel) {
                    const curFree = freeSel.value;
                    const freeModels = models.filter(m=> m.endsWith('-free')).concat(models.filter(m=> !models.includes(m+'-free')).map(m=> m+'-free'));
                    const uniq = [...new Set(freeModels)];
                    freeSel.innerHTML = '<option value="">— choisir —</option>' + uniq.map(m=> `<option value="${escHtml(m)}">${escHtml(m)}</option>`).join('');
                    if (curFree) freeSel.value = curFree;
                }
            } catch (e) { console.error('freeMap parallel', e); }
        } else {
            // Fallback ancien comportement si fetch parallèle a échoué
            try { await refreshFreeModelMap(); } catch (e) {}
        }

        // 6) Rotation Rules
        if (rotationRes.status === 'fulfilled' && rotationRes.value) {
            try {
                _rotationRules = rotationRes.value.rules || [];
                renderRotationRules();
            } catch (e) {}
        } else {
            try { await refreshRotationRules(); } catch (e) {}
        }

        // 7) Schedule
        if (scheduleRes.status === 'fulfilled' && scheduleRes.value) {
            try {
                const data = scheduleRes.value;
                _schedule = data && typeof data.enabled !=='undefined' ? data : {enabled:false, rules: data.rules||[]};
                renderSchedule();
                const tog = document.getElementById('vpn-schedule-enabled');
                if (tog) tog.checked = !!_schedule.enabled;
            } catch (e) {}
        } else {
            try { await refreshSchedule(); } catch (e) {}
        }

        // Fin chargement
        if (fmToggle) fmToggle.disabled = false;
        if (statusEl && statusEl.dataset.loading) {
            delete statusEl.dataset.loading;
            // Laisse updateVPNUI avoir déjà mis le bon label, sinon restaure
            if (statusEl.textContent === 'Chargement...') {
                statusEl.textContent = statusEl.dataset.orig || '—';
                delete statusEl.dataset.orig;
            }
        }
        _vpnRefreshInFlight = false;
    }

    function renderServerList(servers) {
        const list = document.getElementById('vpn-servers-list');
        if (!list) return;
        if (!servers.length) {
            list.innerHTML = '<div style="color:var(--text-muted);font-size:12px;padding:8px">Aucun serveur configuré</div>';
            return;
        }
        list.innerHTML = servers.map((s, i) => `
            <div style="display:flex;justify-content:space-between;align-items:center;padding:6px 8px;background:var(--bg-secondary);border-radius:6px;margin-bottom:4px;font-size:13px">
                <div>
                    <strong>${escHtml(s.name)}</strong>
                    <span style="color:var(--text-muted);margin-left:8px;font-size:11px">${escHtml(s.config || '').split('/').pop()}</span>
                </div>
                <button class="btn btn-sm btn-danger" onclick="vpnRemoveServer('${escHtml(s.name)}')" style="font-size:11px;padding:2px 8px">✕</button>
            </div>
        `).join('');
    }

    function updateVPNUI(data) {
        const statusEl = document.getElementById('vpn-status-text');
        const statusDot = document.getElementById('vpn-status-dot');
        const ipEl = document.getElementById('vpn-ip');
        const serverEl = document.getElementById('vpn-server');
        const requestsEl = document.getElementById('vpn-requests');
        const totalEl = document.getElementById('vpn-total-free');
        const ipsUsedEl = document.getElementById('vpn-ips-used');
        const switchesEl = document.getElementById('vpn-switches');
        const toggleEl = document.getElementById('vpn-toggle');
        const toggleLabel = document.getElementById('vpn-toggle-label');

        if (!statusEl) return;

        const statusMap = {
            connected: { color: 'var(--success)', label: t('vpn.connected') || 'Connecté' },
            connecting: { color: 'var(--warning)', label: t('vpn.connecting') || 'Connexion...' },
            disconnected: { color: 'var(--text-muted)', label: t('vpn.disconnected') || 'Déconnecté' },
            error: { color: 'var(--danger)', label: t('vpn.error') || 'Erreur' }
        };
        const s = statusMap[data.status] || statusMap.disconnected;
        statusEl.textContent = s.label;
        statusEl.style.color = s.color;
        if (statusDot) statusDot.style.background = s.color;

        ipEl.textContent = data.current_ip || '—';
        serverEl.textContent = data.current_server || '—';
        requestsEl.textContent = `${data.requests_this_ip || 0} / ${data.quota_per_ip || 300}`;
        totalEl.textContent = data.total_free_requests || 0;
        if (ipsUsedEl) ipsUsedEl.textContent = data.ips_used || 0;
        if (switchesEl) switchesEl.textContent = (data.vpn && data.vpn.total_switches) || 0;

        toggleEl.checked = data.enabled || false;
        toggleLabel.textContent = data.enabled ? (t('vpn.rotation_on') || 'Activé') : (t('vpn.rotation_off') || 'Désactivé');

        // IP History
        const historyEl = document.getElementById('vpn-ip-history');
        if (historyEl && data.vpn && data.vpn.ip_history) {
            historyEl.innerHTML = data.vpn.ip_history.slice().reverse().map(h =>
                `<div style="padding:3px 0;border-bottom:1px solid var(--border)">
                    <span style="font-family:monospace">${h.ip}</span>
                    <span style="color:var(--text-muted)"> — ${h.server} — ${h.time}</span>
                </div>`
            ).join('') || '<div style="color:var(--text-muted);padding:4px">Aucun historique</div>';
        }

        // Circuit breaker status
        const cbStatusEl = document.getElementById('vpn-cb-status');
        if (cbStatusEl && data.vpn && data.vpn.circuit_breaker) {
            const cb = data.vpn.circuit_breaker;
            const entries = Object.entries(cb);
            if (entries.length > 0) {
                const openServers = entries.filter(([_, v]) => v.state === 'open');
                const failedServers = entries.filter(([_, v]) => v.failures > 0);
                let html = '';
                if (openServers.length > 0) {
                    html += `<span style="color:var(--danger)">🔴 ${openServers.length} serveur(s) bloqué(s)</span> `;
                }
                if (failedServers.length > 0) {
                    html += `<span style="color:var(--warning)">⚠️ ${failedServers.length} serveur(s) avec échecs</span> `;
                }
                if (data.vpn.backoff_failures > 0) {
                    html += `<span style="color:var(--text-muted)">Backoff: ${data.vpn.backoff_delay}s (${data.vpn.backoff_failures} échecs)</span>`;
                }
                cbStatusEl.innerHTML = html || '<span style="color:var(--success)">✓ Tous les serveurs OK</span>';
            } else {
                cbStatusEl.innerHTML = '<span style="color:var(--success)">✓ Aucun échec enregistré</span>';
            }
        }
    }

    window.toggleVPN = async function(enabled) {
        const toggle = document.getElementById('vpn-toggle');
        const prev = toggle ? !enabled : enabled;
        try {
            const resp = await fetch('/api/vpn/toggle', { method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify({enabled}) });
            if (!resp.ok) throw new Error('HTTP '+resp.status);
            const data = await resp.json().catch(()=>({}));
            if (data.error) throw new Error(data.error);
            refreshVPNStatus();
        } catch(e){
            console.error('toggleVPN failed', e);
            if (toggle) toggle.checked = prev;
            const status=document.getElementById('vpn-health-result');
            if(status){ status.innerHTML=`<span style="color:var(--danger)">✗ ${escHtml(e.message)}</span>`; setTimeout(()=>status.textContent='',3000); }
        }
    };

    window.vpnConnect = async function() {
        const resultEl = document.getElementById('vpn-health-result');
        if (resultEl) resultEl.innerHTML = '<span style="color:var(--warning)">Connexion en cours...</span>';

        try {
            const resp = await fetch('/api/vpn/connect', { method: 'POST' });
            const data = await resp.json();
            if (data.error) {
                // Parse error for specific messages
                let msg = data.error;
                let color = 'var(--danger)';
                if (msg.includes('not found') || msg.includes('Not found')) {
                    msg = '✗ NordVPN non installé ou introuvable';
                } else if (msg.includes('AUTH_FAILED') || msg.includes('auth')) {
                    msg = '✗ Échec authentification — vérifiez vos identifiants';
                } else if (msg.includes('locked') || msg.includes('cooldown')) {
                    msg = '✗ Trop de tentatives — attendez avant de réessayer';
                } else if (msg.includes('token')) {
                    msg = '✗ Token NordVPN invalide ou manquant';
                } else if (msg.includes('Config not found')) {
                    msg = '✗ Fichier .ovpn non trouvé';
                } else {
                    msg = '✗ ' + msg;
                }
                if (resultEl) resultEl.innerHTML = `<span style="color:${color}">${msg}</span>`;
            } else {
                const ip = data.ip || '?';
                if (resultEl) resultEl.innerHTML = `<span style="color:var(--success)">✓ Connecté — IP: ${ip}</span>`;
            }
        } catch (e) {
            if (resultEl) resultEl.innerHTML = `<span style="color:var(--danger)">✗ Erreur: ${e.message}</span>`;
        }
        refreshVPNStatus();
    };

    window.vpnSaveConfig = async function() {
        const clamp = (id, min, max, def) => {
            const el = document.getElementById(id);
            if (!el) return def;
            let v = parseInt(el.value);
            if (isNaN(v)) v = def;
            v = Math.max(min, Math.min(max, v));
            el.value = v;
            return v;
        };
        const mode = document.getElementById('vpn-mode').value;
        const proxyPort = clamp('vpn-proxy-port', 1, 65535, 8888);
        const switchDelay = clamp('vpn-switch-delay', 1, 60, 5);
        let dockerImage = document.getElementById('vpn-docker-image')?.value || 'qmcgaw/gluetun';
        if (dockerImage === 'custom') {
            dockerImage = document.getElementById('vpn-docker-image-custom')?.value?.trim() || 'qmcgaw/gluetun';
            if (!dockerImage) return alert('Image Docker requise');
        }
        const cbThreshold = clamp('vpn-cb-threshold', 1, 10, 3);
        const cbRecovery = clamp('vpn-cb-recovery', 30, 3600, 300);
        const backoffMax = clamp('vpn-backoff-max', 10, 300, 60);
        const watchdog = clamp('vpn-watchdog', 0, 600, 60);
        const apiCache = clamp('vpn-api-cache', 60, 7200, 900);

        const statusEl = document.getElementById('vpn-health-result');
        try {
            const resp = await fetch('/api/vpn-config', {
                method: 'POST', headers: {'Content-Type': 'application/json'},
                body: JSON.stringify({
                    mode, proxy_port: proxyPort, switch_delay: switchDelay, docker_image: dockerImage,
                    circuit_breaker_threshold: cbThreshold, circuit_breaker_recovery: cbRecovery,
                    backoff_max_delay: backoffMax, watchdog_interval: watchdog, api_cache_ttl: apiCache
                })
            });
            const data = await resp.json().catch(()=>({}));
            if (!resp.ok || data.error) throw new Error(data.error|| 'HTTP '+resp.status);
            if (statusEl) { statusEl.innerHTML='<span style="color:var(--success)">✓ Sauvegardé (hot-reload)</span>'; setTimeout(()=> statusEl.textContent='',3000); }
        } catch(e){
            console.error('vpnSaveConfig failed', e);
            if (statusEl) { statusEl.innerHTML=`<span style="color:var(--danger)">✗ ${escHtml(e.message)}</span>`; setTimeout(()=> statusEl.textContent='',4000); }
        }

        // Show/hide Docker options (local UI, no fetch rollback needed)
        document.getElementById('vpn-docker-row').style.display = mode === 'docker' ? 'flex' : 'none';
        const hints = { auto: 'Auto-détecte le meilleur mode disponible', docker: 'Docker (reproductible, isolé)', native: 'Windows natif (OpenVPN installé)', 'nordvpn-app': 'NordVPN App (application desktop Windows)', wsl2: 'WSL2 (léger, déjà installé)' };
        const hintEl = document.getElementById('vpn-mode-hint');
        if (hintEl) hintEl.textContent = hints[mode] || hints.wsl2;
    };

    window.saveQuotaPerIp = async function() {
        const el = document.getElementById('vpn-quota-per-ip');
        let quotaPerIp = parseInt(el?.value) || 300;
        quotaPerIp = Math.max(1, Math.min(10000, quotaPerIp));
        if (el) el.value = quotaPerIp;
        const statusEl = document.getElementById('vpn-health-result');
        try {
            const resp = await fetch('/api/vpn-config', {
                method: 'POST', headers: {'Content-Type': 'application/json'},
                body: JSON.stringify({ quota_per_ip: quotaPerIp })
            });
            const data = await resp.json().catch(()=>({}));
            if (!resp.ok || data.error) throw new Error(data.error||'HTTP '+resp.status);
            if (statusEl) { statusEl.innerHTML='<span style="color:var(--success)">✓ Quota sauvegardé</span>'; setTimeout(()=> statusEl.textContent='',2000); }
        } catch(e){
            console.error('saveQuotaPerIp failed', e);
            if (statusEl) { statusEl.innerHTML=`<span style="color:var(--danger)">✗ ${escHtml(e.message)}</span>`; }
        }
    };

    // Initialize mode selector from config
    function vpnInitConfig(mode) {
        document.getElementById('vpn-mode').value = mode || 'wsl2';
        document.getElementById('vpn-docker-row').style.display = (mode === 'docker') ? 'flex' : 'none';
        const nordvpnSection = document.getElementById('vpn-nordvpn-app-section');
        if (nordvpnSection) nordvpnSection.style.display = (mode === 'nordvpn-app') ? 'block' : 'none';
        const hints = {
            auto: 'Auto-détecte le meilleur mode disponible',
            docker: 'Docker (reproductible, isolé)',
            native: 'Windows natif (OpenVPN installé)',
            'nordvpn-app': 'NordVPN App (application desktop Windows)',
            wsl2: 'WSL2 (léger, déjà installé)'
        };
        document.getElementById('vpn-mode-hint').textContent = hints[mode] || hints.wsl2;
    }

    // ── VPN Diagnostic ──
    window.vpnDiagnostic = async function() {
        const resultEl = document.getElementById('vpn-diagnostic-result');
        if (!resultEl) return;
        resultEl.style.display = 'block';
        resultEl.innerHTML = '<span style="color:var(--warning)">Diagnostic en cours...</span>';

        try {
            const resp = await fetch('/api/vpn/diagnostic');
            const data = await resp.json();

            let html = '<strong>🩺 Diagnostic VPN</strong><br>';

            // Current mode
            html += `<div style="margin-top:8px"><strong>Mode actuel:</strong> ${data.current_mode || '?'}</div>`;
            html += `<div><strong>Status:</strong> ${data.status || '?'} | <strong>IP:</strong> ${data.ip || '?'}</div>`;

            // NordVPN App
            const nordvpn = data.nordvpn_app || {};
            const nordvpnIcon = nordvpn.available ? '🟢' : '🔴';
            html += `<div style="margin-top:6px">${nordvpnIcon} <strong>NordVPN App:</strong> ${nordvpn.available ? 'Installé (' + (nordvpn.exe || '?') + ')' : 'Non installé'}`;
            if (nordvpn.status && nordvpn.status.connected) {
                html += ` — Connecté: ${nordvpn.status.country || '?'} / ${nordvpn.status.city || '?'} / ${nordvpn.status.ip || '?'}`;
            }
            html += '</div>';

            // Docker
            const docker = data.docker || {};
            const dockerIcon = docker.running ? '🟢' : docker.available ? '🟡' : '🔴';
            html += `<div>${dockerIcon} <strong>Docker:</strong> ${docker.running ? 'En cours (v' + (docker.version || '?') + ')' : docker.available ? 'Installé mais arrêté' : 'Non installé'}</div>`;

            // WSL2
            const wsl2 = data.wsl2 || {};
            const wsl2Icon = wsl2.available ? '🟢' : '🔴';
            html += `<div>${wsl2Icon} <strong>WSL2:</strong> ${wsl2.available ? 'Disponible' : 'Non disponible'}</div>`;

            // OpenVPN native
            const openvpn = data.openvpn || {};
            const openvpnIcon = openvpn.available ? '🟢' : '🔴';
            html += `<div>${openvpnIcon} <strong>OpenVPN natif:</strong> ${openvpn.available ? openvpn.path : 'Non installé'}</div>`;

            // Recommendation
            html += `<div style="margin-top:8px;padding-top:6px;border-top:1px solid var(--border)"><strong>Recommandation:</strong> <span style="color:var(--success)">${data.recommendation || '?'}</span></div>`;

            resultEl.innerHTML = html;
        } catch (e) {
            resultEl.innerHTML = `<span style="color:var(--danger)">✗ Erreur: ${e.message}</span>`;
        }
    };

    // Test NordVPN App connection
    window.vpnTestNordvpnApp = async function() {
        const statusEl = document.getElementById('vpn-nordvpn-app-status');
        const countriesEl = document.getElementById('vpn-nordvpn-app-countries');
        if (statusEl) statusEl.innerHTML = '<span style="color:var(--warning)">Test en cours...</span>';

        try {
            // Check if available
            const availResp = await fetch('/api/vpn/nordvpn-available');
            const availData = await availResp.json();
            if (!availData.available) {
                if (statusEl) statusEl.innerHTML = '<span style="color:var(--danger)">✗ NordVPN non installé</span>';
                return;
            }

            // Get status
            const statusResp = await fetch('/api/vpn/nordvpn-status');
            const statusData = await statusResp.json();
            if (statusData.connected) {
                if (statusEl) statusEl.innerHTML = `<span style="color:var(--success)">✓ Connecté</span> — ${statusData.country || '?'} / ${statusData.city || '?'} / IP: ${statusData.ip || '?'}`;
            } else {
                if (statusEl) statusEl.innerHTML = '<span style="color:var(--text-muted)">● Déconnecté (prêt)</span>';
            }

            // Get countries count
            const countriesResp = await fetch('/api/vpn/nordvpn-countries');
            const countriesData = await countriesResp.json();
            if (countriesEl && countriesData.countries) {
                countriesEl.textContent = `${countriesData.countries.length} pays disponibles`;
            }
        } catch (e) {
            if (statusEl) statusEl.innerHTML = `<span style="color:var(--danger)">✗ ${e.message}</span>`;
        }
    };

    // Docker image dropdown handler
    window.vpnDockerImageChanged = function() {
        const select = document.getElementById('vpn-docker-image');
        const customInput = document.getElementById('vpn-docker-image-custom');
        const hint = document.getElementById('vpn-docker-image-hint');
        const value = select.value;

        if (value === 'custom') {
            customInput.style.display = 'block';
        } else {
            customInput.style.display = 'none';
        }

        const hints = {
            'qmcgaw/gluetun': 'Gluetun: proxy intégré, WireGuard, 43 MB',
            'nordvpn-official': 'NordVPN Officiel: client natif Linux, construit localement',
            'custom': 'Entrez le nom de l\'image Docker'
        };
        if (hint) hint.textContent = hints[value] || '';
        vpnSaveConfig();
    };

    window.vpnDisconnect = async function() {
        await fetch('/api/vpn/disconnect', { method: 'POST' });
        refreshVPNStatus();
    };

    window.vpnNext = async function() {
        const resp = await fetch('/api/vpn/next', { method: 'POST' });
        const data = await resp.json();
        if (data.error) alert('Erreur: ' + data.error);
        refreshVPNStatus();
    };

    window.vpnRemoveServer = async function(name) {
        if (!confirm(t('vpn.confirm_remove') || 'Supprimer ce serveur ?')) return;
        await fetch('/api/vpn-config', {
            method: 'POST', headers: {'Content-Type': 'application/json'},
            body: JSON.stringify({remove_server: name})
        });
        refreshVPNStatus();
    };

    window.vpnSaveCredentials = async function() {
        const username = document.getElementById('vpn-username').value.trim();
        const password = document.getElementById('vpn-password').value.trim();
        if (!username || !password) return alert('Username and password required');
        const resp = await fetch('/api/vpn-config', {
            method: 'POST', headers: {'Content-Type': 'application/json'},
            body: JSON.stringify({credentials: {username, password}})
        });
        const data = await resp.json();
        if (data.error) {
            alert('Error: ' + data.error);
        } else {
            // Clear fields and show saved indicator
            document.getElementById('vpn-username').value = '';
            document.getElementById('vpn-password').value = '';
            refreshVPNStatus();
        }
    };

    // Store selected file globally (dataset can't hold File objects)
    let _vpnSelectedFile = null;

    window.vpnFileSelected = function(input) {
        const file = input.files[0];
        if (!file) return;
        _vpnSelectedFile = file;
        const el = document.getElementById('vpn-selected-file');
        if (el) el.textContent = 'Fichier: ' + file.name + ' (' + (file.size / 1024).toFixed(1) + ' Ko)';
    };

    window.vpnAddServer = async function() {
        const name = document.getElementById('vpn-server-name').value.trim();
        if (!name) return alert('Nom du serveur requis');

        if (_vpnSelectedFile) {
            // Upload the file
            const formData = new FormData();
            formData.append('name', name);
            formData.append('config', _vpnSelectedFile);

            const resp = await fetch('/api/vpn/upload-config', {
                method: 'POST',
                body: formData
            });
            const data = await resp.json();
            if (data.error) alert('Erreur: ' + data.error);
        } else {
            return alert('Sélectionnez un fichier .ovpn');
        }

        document.getElementById('vpn-server-name').value = '';
        document.getElementById('vpn-selected-file').textContent = '';
        _vpnSelectedFile = null;
        document.getElementById('vpn-file-input').value = '';
        refreshVPNStatus();
    };

    // ── VPN Health Check ──
    window.vpnHealthCheck = async function() {
        const resultEl = document.getElementById('vpn-health-result');
        if (resultEl) resultEl.textContent = 'Test en cours...';
        try {
            const resp = await fetch('/api/vpn/health-check', { method: 'POST' });
            const data = await resp.json();
            if (data.ok) {
                const latency = data.latency_ms ? ` (${data.latency_ms}ms)` : '';
                const changed = data.ip_changed ? ' ⚠️ IP changée' : '';
                if (resultEl) resultEl.innerHTML = `<span style="color:var(--success)">✓ OK${latency}</span>${changed}`;
            } else {
                if (resultEl) resultEl.innerHTML = `<span style="color:var(--danger)">✗ ${data.error || 'Échec'}</span>`;
            }
        } catch (e) {
            if (resultEl) resultEl.innerHTML = `<span style="color:var(--danger)">✗ Erreur: ${e.message}</span>`;
        }
    };

    // ── VPN Save State ──
    window.vpnSaveState = async function() {
        try {
            await fetch('/api/vpn/save-state', { method: 'POST' });
            const resultEl = document.getElementById('vpn-health-result');
            if (resultEl) resultEl.innerHTML = '<span style="color:var(--success)">✓ État sauvegardé</span>';
        } catch (e) {
            console.error('Save state error:', e);
        }
    };

    // ── NordVPN API Toggle ──
    window.vpnToggleApi = async function(enabled) {
        const section = document.getElementById('vpn-api-section');
        if (section) section.style.display = enabled ? 'block' : 'none';
        await fetch('/api/vpn-config', {
            method: 'POST', headers: {'Content-Type': 'application/json'},
            body: JSON.stringify({ use_nordvpn_api: enabled })
        });
        if (enabled) {
            vpnLoadCountries();
        }
    };

    // ── Load Countries for API ──
    async function vpnLoadCountries() {
        try {
            const resp = await fetch('/api/vpn/countries');
            const data = await resp.json();
            const select = document.getElementById('vpn-api-country');
            if (select && data.countries) {
                select.innerHTML = '<option value="">Tous</option>' +
                    data.countries.map(c => `<option value="${c.code}">${c.name} (${c.code})</option>`).join('');
            }
        } catch (e) {
            console.error('Load countries error:', e);
        }
    }

    // ── Discover Servers via API ──
    window.vpnDiscoverServers = async function() {
        const country = document.getElementById('vpn-api-country')?.value || '';
        const group = document.getElementById('vpn-api-group')?.value || '';
        const limit = parseInt(document.getElementById('vpn-api-limit')?.value) || 5;
        const resultsEl = document.getElementById('vpn-api-results');
        if (resultsEl) resultsEl.textContent = 'Découverte en cours...';

        try {
            const resp = await fetch('/api/vpn/discover-and-add', {
                method: 'POST', headers: {'Content-Type': 'application/json'},
                body: JSON.stringify({ country: country || null, group: group || null, limit })
            });
            const data = await resp.json();
            if (data.error) {
                if (resultsEl) resultsEl.innerHTML = `<span style="color:var(--danger)">✗ ${data.error}</span>`;
            } else {
                if (resultsEl) resultsEl.innerHTML = `<span style="color:var(--success)">✓ ${data.count} serveur(s) ajouté(s)</span>`;
                refreshVPNStatus();
            }
        } catch (e) {
            if (resultsEl) resultsEl.innerHTML = `<span style="color:var(--danger)">✗ Erreur: ${e.message}</span>`;
        }
    };

    // ── Export VPN Config ──
    window.vpnExportConfig = async function() {
        try {
            const resp = await fetch('/api/vpn/export');
            const data = await resp.json();
            const blob = new Blob([JSON.stringify(data, null, 2)], { type: 'application/json' });
            const url = URL.createObjectURL(blob);
            const a = document.createElement('a');
            a.href = url;
            a.download = `opencode-vpn-config-${new Date().toISOString().slice(0,10)}.json`;
            a.click();
            URL.revokeObjectURL(url);
        } catch (e) {
            alert('Export failed: ' + e.message);
        }
    };

    // ── Import VPN Config ──
    window.vpnImportConfig = async function() {
        const input = document.createElement('input');
        input.type = 'file';
        input.accept = '.json';
        input.onchange = async (e) => {
            const file = e.target.files[0];
            if (!file) return;
            try {
                const text = await file.text();
                const data = JSON.parse(text);
                const resp = await fetch('/api/vpn/import', {
                    method: 'POST', headers: {'Content-Type': 'application/json'},
                    body: JSON.stringify(data)
                });
                const result = await resp.json();
                if (result.ok) {
                    alert('Configuration importée avec succès');
                    refreshVPNStatus();
                } else {
                    alert('Import failed: ' + (result.error || 'Unknown error'));
                }
            } catch (e) {
                alert('Import failed: ' + e.message);
            }
        };
        input.click();
    };

    // ── Free Models master toggle (robust, rollback on error) ──
    window.toggleFreeModels = async function(enabled) {
        const toggle = document.getElementById('fm-toggle');
        const prev = toggle ? !enabled : enabled;
        try {
            const resp = await fetch('/api/vpn/toggle', {
                method: 'POST', headers: {'Content-Type': 'application/json'},
                body: JSON.stringify({enabled})
            });
            if (!resp.ok) throw new Error('HTTP ' + resp.status);
            const data = await resp.json().catch(()=>({}));
            if (data.error) throw new Error(data.error);
            refreshVPNStatus();
        } catch (e) {
            console.error('toggleFreeModels failed:', e);
            if (toggle) toggle.checked = prev;
            const status = document.getElementById('vpn-health-result');
            if (status) { status.innerHTML = `<span style="color:var(--danger)">✗ ${escHtml(e.message)}</span>`; setTimeout(()=> status.textContent='', 3000); }
        }
    };

    // ── Proxy mode switching (robust, rollback) ──
    function setProxyModeUI(mode) {
        // Update radio buttons
        document.querySelectorAll('.proxy-mode-btn').forEach(btn => btn.classList.remove('active'));
        const activeBtn = document.getElementById('fm-mode-' + mode);
        if (activeBtn) activeBtn.classList.add('active');

        // Show/hide sections
        const vpnSection = document.getElementById('fm-vpn-section');
        const socks5Section = document.getElementById('fm-socks5-section');
        if (vpnSection) vpnSection.style.display = mode === 'vpn' ? 'block' : 'none';
        if (socks5Section) socks5Section.style.display = mode === 'socks5' ? 'block' : 'none';
    }

    window.switchProxyMode = async function(mode) {
        // Capture previous mode for rollback
        const prevMode = document.querySelector('.proxy-mode-btn.active')?.id?.replace('fm-mode-','') || 'vpn';
        setProxyModeUI(mode);
        try {
            const resp = await fetch('/api/vpn/proxy-mode', {
                method: 'POST', headers: {'Content-Type': 'application/json'},
                body: JSON.stringify({mode})
            });
            if (!resp.ok) throw new Error('HTTP ' + resp.status);
            const data = await resp.json().catch(()=>({}));
            if (data.error) throw new Error(data.error);
            refreshVPNStatus();
        } catch (e) {
            console.error('switchProxyMode failed:', e);
            setProxyModeUI(prevMode);
            const status = document.getElementById('vpn-health-result');
            if (status) { status.innerHTML = `<span style="color:var(--danger)">✗ ${escHtml(e.message)}</span>`; setTimeout(()=> status.textContent='', 3000); }
        }
    };

    // ── Free Model Map (hot-reload) ──
    let _freeModelMap = {};
    async function refreshFreeModelMap() {
        try {
            const resp = await fetch('/api/config/free-model-map');
            const data = await resp.json();
            _freeModelMap = data.free_model_map || {};
            renderFreeModelMap();
            // Populate selects for adding
            const paidSel = document.getElementById('fmm-paid');
            const freeSel = document.getElementById('fmm-free');
            const models = data.available_models || availableModels || [];
            if (paidSel) {
                const curPaid = paidSel.value;
                paidSel.innerHTML = '<option value="">— choisir —</option>' + models.map(m=> `<option value="${escHtml(m)}">${escHtml(m)}</option>`).join('');
                if (curPaid) paidSel.value = curPaid;
            }
            if (freeSel) {
                const curFree = freeSel.value;
                const freeModels = models.filter(m=> m.endsWith('-free')).concat(models.filter(m=> !models.includes(m+'-free')).map(m=> m+'-free'));
                // dedup
                const uniq = [...new Set(freeModels)];
                freeSel.innerHTML = '<option value="">— choisir —</option>' + uniq.map(m=> `<option value="${escHtml(m)}">${escHtml(m)}</option>`).join('');
                if (curFree) freeSel.value = curFree;
            }
        } catch (e) { console.error('refreshFreeModelMap failed', e); }
    }
    function renderFreeModelMap() {
        const tbody = document.getElementById('free-model-map-tbody');
        if (!tbody) return;
        const entries = Object.entries(_freeModelMap);
        if (!entries.length) {
            tbody.innerHTML = '<tr><td colspan="3" style="color:var(--text-muted);padding:8px">Aucun mapping (tous les modèles passent en payant)</td></tr>';
            return;
        }
        tbody.innerHTML = entries.map(([paid, free])=> `
            <tr>
                <td style="font-family:monospace">${escHtml(paid)}</td>
                <td style="font-family:monospace">${escHtml(free)}</td>
                <td><button class="btn btn-sm btn-danger" onclick="removeFreeModelMapEntry('${escHtml(paid)}')" style="font-size:11px;padding:2px 6px">✕</button></td>
            </tr>
        `).join('');
    }
    window.addFreeModelMap = function() {
        const paid = document.getElementById('fmm-paid')?.value?.trim();
        const free = document.getElementById('fmm-free')?.value?.trim();
        if (!paid || !free) return alert('Choisissez payant et gratuit');
        _freeModelMap[paid] = free;
        renderFreeModelMap();
        document.getElementById('fmm-paid').value = '';
        document.getElementById('fmm-free').value = '';
    };
    window.removeFreeModelMapEntry = function(paid) {
        if (!confirm('Supprimer ce mapping ?')) return;
        delete _freeModelMap[paid];
        renderFreeModelMap();
    };
    window.saveFreeModelMap = async function() {
        const status = document.getElementById('fmm-save-status');
        if (status) status.textContent = 'Sauvegarde...';
        try {
            const resp = await fetch('/api/config/free-model-map', {
                method: 'POST', headers: {'Content-Type': 'application/json'},
                body: JSON.stringify({free_model_map: _freeModelMap})
            });
            const data = await resp.json();
            if (!resp.ok || data.error) throw new Error(data.error || 'HTTP '+resp.status);
            _freeModelMap = data.free_model_map || _freeModelMap;
            renderFreeModelMap();
            if (status) { status.textContent = '✓ Sauvegardé (hot-reload)'; status.style.color='var(--success)'; setTimeout(()=> status.textContent='',3000); }
        } catch (e) {
            console.error('saveFreeModelMap failed', e);
            if (status) { status.textContent = '✗ ' + e.message; status.style.color='var(--danger)'; }
        }
    };

    // ── Rotation Rules (hot-reload) ──
    let _rotationRules = [];
    async function refreshRotationRules() {
        try {
            const resp = await fetch('/api/vpn/rotation-rules');
            const data = await resp.json();
            _rotationRules = data.rules || [];
            renderRotationRules();
        } catch(e){ console.error('refreshRotationRules',e); }
    }
    function renderRotationRules() {
        const el = document.getElementById('vpn-rotation-rules-list');
        if (!el) return;
        if (!_rotationRules.length) { el.innerHTML = '<div style="color:var(--text-muted);padding:6px">Aucune règle (rotation par défaut)</div>'; return; }
        el.innerHTML = _rotationRules.map((r,i)=> `
            <div style="display:flex;justify-content:space-between;align-items:center;padding:6px 8px;background:var(--bg);border-radius:6px;margin-bottom:4px">
                <span><code>${escHtml(r.model_pattern||r.pattern||'')}</code> → ${escHtml(r.strategy||'round-robin')} ${r.quota? '('+r.quota+')':''} ${r.countries? '['+ (r.countries||[]).join(',')+']':''}</span>
                <button class="btn btn-sm btn-danger" onclick="removeRotationRule(${i})" style="font-size:11px;padding:2px 6px">✕</button>
            </div>
        `).join('');
    }
    window.addRotationRule = function() {
        const pattern = document.getElementById('rr-pattern')?.value?.trim();
        const strategy = document.getElementById('rr-strategy')?.value || 'round-robin';
        const quota = parseInt(document.getElementById('rr-quota')?.value) || undefined;
        if (!pattern) return alert('Pattern requis');
        const rule = {model_pattern: pattern, strategy};
        if (quota) rule.quota = quota;
        _rotationRules.push(rule);
        renderRotationRules();
        document.getElementById('rr-pattern').value=''; document.getElementById('rr-quota').value='';
    };
    window.removeRotationRule = function(idx) {
        if (!confirm('Supprimer cette règle ?')) return;
        _rotationRules.splice(idx,1);
        renderRotationRules();
    };
    window.saveRotationRules = async function() {
        const status = document.getElementById('rr-save-status');
        if (status) status.textContent='Sauvegarde...';
        try {
            const resp = await fetch('/api/vpn/rotation-rules', {method:'POST', headers:{'Content-Type':'application/json'}, body: JSON.stringify({rules:_rotationRules})});
            const data = await resp.json();
            if (!resp.ok || data.error) throw new Error(data.error||'HTTP '+resp.status);
            _rotationRules = data.rules||_rotationRules;
            renderRotationRules();
            if (status){ status.textContent='✓ Sauvegardé (hot-reload)'; status.style.color='var(--success)'; setTimeout(()=> status.textContent='',3000); }
        } catch(e){ console.error(e); if(status){ status.textContent='✗ '+e.message; status.style.color='var(--danger)'; } }
    };

    // ── Schedule (hot-reload) ──
    let _schedule = {enabled:false, rules:[]};
    async function refreshSchedule() {
        try {
            const resp = await fetch('/api/vpn/schedule');
            const data = await resp.json();
            _schedule = data && typeof data.enabled !=='undefined' ? data : {enabled:false, rules: data.rules||[]};
            renderSchedule();
            const tog = document.getElementById('vpn-schedule-enabled');
            if (tog) tog.checked = !!_schedule.enabled;
        } catch(e){ console.error('refreshSchedule',e); }
    }
    function renderSchedule() {
        const el = document.getElementById('vpn-schedule-list');
        if (!el) return;
        const rules = _schedule.rules||[];
        if (!rules.length) { el.innerHTML='<div style="color:var(--text-muted);padding:6px">Aucun planning</div>'; return; }
        el.innerHTML = rules.map((r,i)=> `
            <div style="display:flex;justify-content:space-between;align-items:center;padding:6px 8px;background:var(--bg);border-radius:6px;margin-bottom:4px">
                <span><code>${escHtml(r.cron||'')}</code> → ${escHtml(r.action||'rotate')}</span>
                <button class="btn btn-sm btn-danger" onclick="removeScheduleRule(${i})" style="font-size:11px;padding:2px 6px">✕</button>
            </div>
        `).join('');
    }
    window.addScheduleRule = function() {
        const cron = document.getElementById('sched-cron')?.value?.trim();
        const action = document.getElementById('sched-action')?.value || 'rotate';
        if (!cron) return alert('Cron requis (ex: 0 */2 * * *)');
        const parts = cron.split(/\s+/);
        if (parts.length!==5) return alert('Cron doit avoir 5 champs');
        if (!_schedule.rules) _schedule.rules=[];
        _schedule.rules.push({cron, action});
        renderSchedule();
        document.getElementById('sched-cron').value='';
    };
    window.removeScheduleRule = function(idx){ if(!confirm('Supprimer ?')) return; _schedule.rules.splice(idx,1); renderSchedule(); };
    window.toggleSchedule = function(enabled){ _schedule.enabled = !!enabled; };
    window.saveSchedule = async function(){
        const status=document.getElementById('sched-save-status');
        if(status) status.textContent='Sauvegarde...';
        try{
            const resp=await fetch('/api/vpn/schedule',{method:'POST', headers:{'Content-Type':'application/json'}, body: JSON.stringify(_schedule)});
            const data=await resp.json();
            if(!resp.ok||data.error) throw new Error(data.error||'HTTP '+resp.status);
            _schedule=data.schedule||_schedule;
            renderSchedule();
            if(status){ status.textContent='✓ Sauvegardé (hot-reload)'; status.style.color='var(--success)'; setTimeout(()=>status.textContent='',3000); }
        }catch(e){ console.error(e); if(status){ status.textContent='✗ '+e.message; status.style.color='var(--danger)'; } }
    };

    // ── SOCKS5 proxy management ──
    function renderSocks5List(proxies) {
        const list = document.getElementById('fm-socks5-list');
        if (!list) return;
        if (!proxies.length) {
            list.innerHTML = '<div style="color:var(--text-muted);font-size:12px;padding:8px">Aucun proxy configuré</div>';
            return;
        }
        list.innerHTML = proxies.map((p, i) => `
            <div class="socks5-item" style="display:flex;justify-content:space-between;align-items:center;padding:8px 12px;background:var(--bg-secondary);border-radius:6px;margin-bottom:4px;font-size:13px">
                <div style="display:flex;align-items:center;gap:12px">
                    <label class="toggle-switch" style="margin:0">
                        <input type="checkbox" ${p.enabled ? 'checked' : ''} onchange="toggleSocks5Proxy(${i}, this.checked)">
                        <span class="toggle-slider"></span>
                    </label>
                    <span style="font-family:monospace;font-weight:bold">${escHtml(p.host)}:${escHtml(String(p.port))}</span>
                    ${p.has_password ? '<span style="font-size:11px;color:var(--text-muted)">🔒</span>' : ''}
                </div>
                <div style="display:flex;gap:4px">
                    <button class="btn btn-sm" onclick="testSocks5Proxy('${escHtml(p.host)}', ${p.port})" style="font-size:11px;padding:2px 8px" data-i18n="free_models.socks5_test">Tester</button>
                    <button class="btn btn-sm btn-danger" onclick="removeSocks5Proxy(${i})" style="font-size:11px;padding:2px 8px">✕</button>
                </div>
            </div>
        `).join('');
    }

    window.addSocks5Proxy = async function() {
        const host = document.getElementById('fm-socks5-host').value.trim();
        const port = parseInt(document.getElementById('fm-socks5-port').value) || 1080;
        const username = document.getElementById('fm-socks5-user').value.trim();
        const password = document.getElementById('fm-socks5-pass').value.trim();

        if (!host) return alert('Hôte requis');

        const resp = await fetch('/api/vpn/socks5', {
            method: 'POST', headers: {'Content-Type': 'application/json'},
            body: JSON.stringify({host, port, username, password})
        });
        const data = await resp.json();
        if (data.error) return alert('Erreur: ' + data.error);

        // Clear form
        document.getElementById('fm-socks5-host').value = '';
        document.getElementById('fm-socks5-port').value = '1080';
        document.getElementById('fm-socks5-user').value = '';
        document.getElementById('fm-socks5-pass').value = '';

        renderSocks5List(data.proxies || []);
    };

    window.removeSocks5Proxy = async function(index) {
        if (!confirm(t('free_models.socks5_remove') || 'Supprimer ce proxy ?')) return;

        const resp = await fetch('/api/vpn/socks5/remove', {
            method: 'POST', headers: {'Content-Type': 'application/json'},
            body: JSON.stringify({index})
        });
        const data = await resp.json();
        renderSocks5List(data.proxies || []);
    };

    window.toggleSocks5Proxy = async function(index, enabled) {
        await fetch('/api/vpn/socks5/toggle', {
            method: 'POST', headers: {'Content-Type': 'application/json'},
            body: JSON.stringify({index, enabled})
        });
    };

    window.testSocks5Proxy = async function(host, port) {
        const btn = event.target;
        const origText = btn.textContent;
        btn.textContent = t('free_models.socks5_testing') || 'Test...';
        btn.disabled = true;

        try {
            const resp = await fetch('/api/vpn/socks5/test', {
                method: 'POST', headers: {'Content-Type': 'application/json'},
                body: JSON.stringify({host, port})
            });
            const data = await resp.json();

            if (data.ok) {
                btn.textContent = '✓ ' + (data.ip || 'OK');
                btn.style.color = 'var(--success)';
                if (data.opencode_ok) {
                    btn.title = 'opencode.ai accessible';
                } else {
                    btn.title = 'ipify OK, opencode.ai inaccessible';
                }
            } else {
                btn.textContent = '✕ ' + (data.error || (t('free_models.socks5_fail') || 'Échec'));
                btn.style.color = 'var(--danger)';
            }
        } catch (e) {
            btn.textContent = '✕ Erreur';
            btn.style.color = 'var(--danger)';
        }

        btn.disabled = false;
        setTimeout(() => {
            btn.textContent = origText;
            btn.style.color = '';
            btn.title = '';
        }, 3000);
    };

    window.toggleSocks5Rotation = async function(enabled) {
        await fetch('/api/vpn/socks5/rotate', {
            method: 'POST', headers: {'Content-Type': 'application/json'},
            body: JSON.stringify({rotate: enabled})
        });
    };

    // Refresh VPN status every 10 seconds when VPN tab is active
    setInterval(() => {
        const vpnTab = document.querySelector('.tab[data-tab="vpn"]');
        if (vpnTab && vpnTab.classList.contains('active')) {
            refreshVPNStatus();
        }
    }, 10000);
});