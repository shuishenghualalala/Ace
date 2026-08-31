import type { IconId } from '../components/icon';
import type { ProductMode } from '../stores/product-mode-store';

export type WorkspaceModuleKind = 'conversation' | 'companion' | 'external' | 'wiki' | 'project';

export interface WorkspaceNavigationItem {
  id: string;
  label: string;
  icon: IconId;
  productModes: readonly ProductMode[];
  order: number;
}

export interface WorkspaceModule {
  id: string;
  kind: WorkspaceModuleKind;
  label: string;
  navigation?: WorkspaceNavigationItem;
  dispose?: () => void;
}

export class WorkspaceRegistry {
  private readonly modules = new Map<string, WorkspaceModule>();

  register(module: WorkspaceModule): () => void {
    if (this.modules.has(module.id)) throw new Error(`Workspace module already registered: ${module.id}`);
    this.modules.set(module.id, module);
    return () => this.unregister(module.id);
  }

  unregister(id: string): boolean {
    const module = this.modules.get(id);
    if (!module) return false;
    module.dispose?.();
    return this.modules.delete(id);
  }

  get(id: string): WorkspaceModule | undefined {
    return this.modules.get(id);
  }

  has(id: string): boolean {
    return this.modules.has(id);
  }

  list(): WorkspaceModule[] {
    return [...this.modules.values()];
  }

  navigation(productMode: ProductMode): WorkspaceNavigationItem[] {
    return this.list()
      .map((module) => module.navigation)
      .filter((item): item is WorkspaceNavigationItem => Boolean(item && item.productModes.includes(productMode)))
      .sort((left, right) => left.order - right.order);
  }
}

export function createDesktopWorkspaceRegistry(): WorkspaceRegistry {
  const registry = new WorkspaceRegistry();
  registry.register({
    id: 'conversation',
    kind: 'conversation',
    label: '普通对话',
    navigation: { id: 'chat', label: '对话', icon: 'process-thinking', productModes: ['assistant'], order: 10 },
  });
  registry.register({
    id: 'external',
    kind: 'external',
    label: '外援',
    navigation: { id: 'agents', label: '外援', icon: 'icon-external-agent', productModes: ['assistant'], order: 20 },
  });
  registry.register({
    id: 'wiki',
    kind: 'wiki',
    label: 'Wiki',
    navigation: { id: 'wiki', label: 'Wiki', icon: 'icon-wiki', productModes: ['assistant', 'work'], order: 50 },
  });
  registry.register({
    id: 'companion',
    kind: 'companion',
    label: '同伴',
    navigation: { id: 'nearby', label: '同伴', icon: 'icon-team', productModes: ['assistant'], order: 60 },
  });
  registry.register({
    id: 'project',
    kind: 'project',
    label: '项目工作空间',
    navigation: { id: 'workbench', label: '工作', icon: 'icon-task', productModes: ['work'], order: 10 },
  });
  return registry;
}
