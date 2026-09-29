package com.alysis.ide;

import com.intellij.openapi.project.DumbAware;
import com.intellij.openapi.project.Project;
import com.intellij.openapi.wm.ToolWindow;
import com.intellij.openapi.wm.ToolWindowFactory;
import com.intellij.ui.content.ContentFactory;
import com.intellij.ui.jcef.JBCefApp;
import javax.swing.JLabel;

public final class AlysisToolWindowFactory implements ToolWindowFactory, DumbAware {
    @Override public void createToolWindowContent(Project project, ToolWindow toolWindow) {
        if (!JBCefApp.isSupported()) {
            toolWindow.getContentManager().addContent(ContentFactory.getInstance().createContent(
                new JLabel("Alysis Code requires the IDE's bundled JetBrains Runtime with JCEF."), "", false));
            return;
        }
        AlysisPanel panel = new AlysisPanel(project);
        var content = ContentFactory.getInstance().createContent(panel.component(), "", false);
        content.setDisposer(panel);
        toolWindow.getContentManager().addContent(content);
    }
}
